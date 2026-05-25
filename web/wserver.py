# ruff: noqa: E402
from uvloop import install

install()

from asyncio import sleep
from urllib.parse import urlparse
from base64 import urlsafe_b64decode
from hashlib import sha256
from hmac import compare_digest, new as hmac_new
from contextlib import asynccontextmanager
from logging import INFO, WARNING, FileHandler, StreamHandler, basicConfig, getLogger

from aioaria2 import Aria2HttpClient
from aiohttp.client_exceptions import ClientError
from aioqbt.client import create_client
from fastapi import FastAPI, Request, HTTPException
from fastapi import Form
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sabnzbdapi import SabnzbdClient
from aioaria2 import Aria2HttpClient
from aioqbt.client import create_client
from aiohttp.client_exceptions import ClientError
from aioqbt.exc import AQError

from web.nodes import extract_file_ids, make_tree
from aiohttp import ClientSession
from pyrogram import Client as PyroClient
from pyrogram.errors import SessionPasswordNeeded
from secrets import token_urlsafe
from time import time
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.server_api import ServerApi

from bot.core.config_manager import Config

getLogger("httpx").setLevel(WARNING)
getLogger("aiohttp").setLevel(WARNING)

aria2 = None
qbittorrent = None
sabnzbd_client = SabnzbdClient(
    host="http://localhost",
    api_key="admin",
    port="8070",
)
SERVICES = {
    "nzb": {"url": "http://localhost:8070/"},
    "qbit": {"url": "http://localhost:8090", "password": "wzmlx"},
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    global aria2, qbittorrent
    aria2 = Aria2HttpClient("http://localhost:6800/jsonrpc")
    qbittorrent = await create_client("http://localhost:8090/api/v2/")
    yield
    await aria2.close()
    await qbittorrent.close()


app = FastAPI(lifespan=lifespan)


templates = Jinja2Templates(directory="web/templates/")

basicConfig(
    format="[%(asctime)s] [%(levelname)s] - %(message)s",  #  [%(filename)s:%(lineno)d]
    datefmt="%d-%b-%y %I:%M:%S %p",
    handlers=[FileHandler("log.txt"), StreamHandler()],
    level=INFO,
)

LOGGER = getLogger(__name__)
tg_session_jobs = {}


def parse_tg_session_token(token: str):
    try:
        raw = urlsafe_b64decode(token.encode()).decode()
        user_id, exp, sig = raw.split(":", 2)
        payload = f"{user_id}:{exp}"
        expected = hmac_new(Config.BOT_TOKEN.encode(), payload.encode(), sha256).hexdigest()
        if not compare_digest(sig, expected):
            return None
        if int(exp) < int(time()):
            return None
        return int(user_id)
    except Exception:
        return None


async def re_verify(paused, resumed, hash_id):
    k = 0
    while True:
        res = await qbittorrent.torrents.files(hash_id)
        verify = True
        for i in res:
            if i.index in paused and i.priority != 0:
                verify = False
                break
            if i.index in resumed and i.priority == 0:
                verify = False
                break
        if verify:
            break
        LOGGER.info("Reverification Failed! Correcting stuff...")
        await sleep(0.5)
        if paused:
            try:
                await qbittorrent.torrents.file_prio(
                    hash=hash_id, id=paused, priority=0
                )
            except (ClientError, TimeoutError, Exception, AQError) as e:
                LOGGER.error(f"{e} Errored in reverification paused!")
        if resumed:
            try:
                await qbittorrent.torrents.file_prio(
                    hash=hash_id, id=resumed, priority=1
                )
            except (ClientError, TimeoutError, Exception, AQError) as e:
                LOGGER.error(f"{e} Errored in reverification resumed!")
        k += 1
        if k > 5:
            return False
    LOGGER.info(f"Verified! Hash: {hash_id}")
    return True


@app.get("/app/files", response_class=HTMLResponse)
async def files(request: Request):
    return templates.TemplateResponse(request=request, name="page.html")


@app.api_route(
    "/app/files/torrent", methods=["GET", "POST"], response_class=HTMLResponse
)
async def handle_torrent(request: Request):
    params = request.query_params

    if not (gid := params.get("gid")):
        return JSONResponse(
            {
                "files": [],
                "engine": "",
                "error": "GID is missing",
                "message": "GID not specified",
            }
        )

    if not (pin := params.get("pin")):
        return JSONResponse(
            {
                "files": [],
                "engine": "",
                "error": "Pin is missing",
                "message": "PIN not specified",
            }
        )

    code = "".join([nbr for nbr in gid if nbr.isdigit()][:4])
    if code != pin:
        return JSONResponse(
            {
                "files": [],
                "engine": "",
                "error": "Invalid pin",
                "message": "The PIN you entered is incorrect",
            }
        )

    if request.method == "POST":
        if not (mode := params.get("mode")):
            return JSONResponse(
                {
                    "files": [],
                    "engine": "",
                    "error": "Mode is not specified",
                    "message": "Mode is not specified",
                }
            )
        data = await request.json()
        if mode == "rename":
            if len(gid) > 20:
                await handle_rename(gid, data)
                content = {
                    "files": [],
                    "engine": "",
                    "error": "",
                    "message": "Rename successfully.",
                }
            else:
                content = {
                    "files": [],
                    "engine": "",
                    "error": "Rename failed.",
                    "message": "Cannot rename aria2c torrent file",
                }
        else:
            selected_files, unselected_files = extract_file_ids(data)
            if gid.startswith("SABnzbd_nzo"):
                await set_sabnzbd(gid, unselected_files)
            elif len(gid) > 20:
                await set_qbittorrent(gid, selected_files, unselected_files)
            else:
                selected_files = ",".join(selected_files)
                await set_aria2(gid, selected_files)
            content = {
                "files": [],
                "engine": "",
                "error": "",
                "message": "Your selection has been submitted successfully.",
            }
    else:
        try:
            if gid.startswith("SABnzbd_nzo"):
                res = await sabnzbd_client.get_files(gid)
                content = make_tree(res, "sabnzbd")
            elif len(gid) > 20:
                res = await qbittorrent.torrents.files(gid)
                content = make_tree(res, "qbittorrent")
            else:
                res = await aria2.getFiles(gid)
                op = await aria2.getOption(gid)
                fpath = f"{op['dir']}/"
                content = make_tree(res, "aria2", fpath)
        except (ClientError, TimeoutError, Exception, AQError) as e:
            LOGGER.error(str(e))
            content = {
                "files": [],
                "engine": "",
                "error": "Error getting files",
                "message": str(e),
            }
    return JSONResponse(content)


async def handle_rename(gid, data):
    try:
        _type = data["type"]
        del data["type"]
        if _type == "file":
            await qbittorrent.torrents.rename_file(hash=gid, **data)
        else:
            await qbittorrent.torrents.rename_folder(hash=gid, **data)
    except (ClientError, TimeoutError, Exception, AQError) as e:
        LOGGER.error(f"{e} Errored in renaming")


async def set_sabnzbd(gid, unselected_files):
    await sabnzbd_client.remove_file(gid, unselected_files)
    LOGGER.info(f"Verified! nzo_id: {gid}")


async def set_qbittorrent(gid, selected_files, unselected_files):
    if unselected_files:
        try:
            await qbittorrent.torrents.file_prio(
                hash=gid, id=unselected_files, priority=0
            )
        except (ClientError, TimeoutError, Exception, AQError) as e:
            LOGGER.error(f"{e} Errored in paused")
    if selected_files:
        try:
            await qbittorrent.torrents.file_prio(
                hash=gid, id=selected_files, priority=1
            )
        except (ClientError, TimeoutError, Exception, AQError) as e:
            LOGGER.error(f"{e} Errored in resumed")
    await sleep(0.5)
    if not await re_verify(unselected_files, selected_files, gid):
        LOGGER.error(f"Verification Failed! Hash: {gid}")


async def set_aria2(gid, selected_files):
    res = await aria2.changeOption(gid, {"select-file": selected_files})
    if res == "OK":
        LOGGER.info(f"Verified! Gid: {gid}")
    else:
        LOGGER.info(f"Verification Failed! Report! Gid: {gid}")


@app.get("/", response_class=HTMLResponse)
async def homepage(request: Request):
    return templates.TemplateResponse(request=request, name="landing.html")


@app.get("/app/tg-session", response_class=HTMLResponse)
async def tg_session_page(token: str = ""):
    user_id = parse_tg_session_token(token) if token else None
    if not user_id:
        return HTMLResponse("<h3>Invalid or expired token.</h3>", status_code=400)
    tg_session_jobs[token] = {"user_id": user_id}
    return HTMLResponse(
        f"""
<html><body>
<h3>Generate Telegram Session</h3>
<form method="post" action="/app/tg-session/start">
<input type="hidden" name="token" value="{token}">
<p>API ID: <input name="api_id" required></p>
<p>API HASH: <input name="api_hash" required></p>
<p>PHONE: <input name="phone" required placeholder="+1234567890"></p>
<button type="submit">Send OTP</button>
</form>
</body></html>
"""
    )


@app.post("/app/tg-session/start", response_class=HTMLResponse)
async def tg_session_start(
    token: str = Form(...), api_id: str = Form(...), api_hash: str = Form(...), phone: str = Form(...)
):
    if token not in tg_session_jobs:
        return HTMLResponse("<h3>Invalid or expired token.</h3>", status_code=400)
    if not api_id.isdigit():
        return HTMLResponse("<h3>API ID must be numeric.</h3>", status_code=400)
    job = tg_session_jobs[token]
    client = PyroClient(
        name=f"WZ-Web-{job['user_id']}",
        api_id=int(api_id),
        api_hash=api_hash,
        phone_number=phone,
        in_memory=True,
        no_updates=True,
    )
    try:
        await client.connect()
        sent = await client.send_code(phone)
        job.update(
            {
                "client": client,
                "phone": phone,
                "phone_code_hash": sent.phone_code_hash,
            }
        )
        return HTMLResponse(
            f"""
<html><body>
<h3>Enter OTP Code</h3>
<form method="post" action="/app/tg-session/verify">
<input type="hidden" name="token" value="{token}">
<p>OTP: <input name="otp" required placeholder="12345"></p>
<button type="submit">Verify</button>
</form>
</body></html>
"""
        )
    except Exception as e:
        try:
            await client.disconnect()
        except Exception:
            pass
        return HTMLResponse(f"<h3>Failed to send OTP: {e}</h3>", status_code=400)


@app.post("/app/tg-session/verify", response_class=HTMLResponse)
async def tg_session_verify(token: str = Form(...), otp: str = Form(...), password: str = Form(None)):
    if token not in tg_session_jobs:
        return HTMLResponse("<h3>Invalid or expired token.</h3>", status_code=400)
    job = tg_session_jobs[token]
    client = job.get("client")
    if not client:
        return HTMLResponse("<h3>Session not initialized.</h3>", status_code=400)
    try:
        try:
            await client.sign_in(job["phone"], job["phone_code_hash"], otp.replace(" ", ""))
        except SessionPasswordNeeded:
            if not password:
                return HTMLResponse(
                    f"""
<html><body>
<h3>2FA Password Required</h3>
<form method="post" action="/app/tg-session/verify">
<input type="hidden" name="token" value="{token}">
<input type="hidden" name="otp" value="{otp}">
<p>Password: <input name="password" required type="password"></p>
<button type="submit">Submit Password</button>
</form>
</body></html>
"""
                )
            await client.check_password(password)
        session_string = await client.export_session_string()
        user_id = job["user_id"]
        db_id = Config.BOT_TOKEN.split(":", 1)[0]
        conn = AsyncIOMotorClient(Config.DATABASE_URL, server_api=ServerApi("1"))
        try:
            db = conn.wzmlx
            await db.users[db_id].update_one(
                {"_id": user_id},
                {
                    "$set": {
                        "OWN_TG_SESSION": session_string,
                        "USE_OWN_TG_SESSION": True,
                        "USER_TRANSMISSION": True,
                    }
                },
                upsert=True,
            )
        finally:
            conn.close()
        return HTMLResponse(
            "<h3>Session generated and auto-saved successfully.</h3>"
        )
    except Exception as e:
        return HTMLResponse(f"<h3>Failed to generate session: {e}</h3>", status_code=400)
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
        tg_session_jobs.pop(token, None)


def rewrite_location(location: str, proxy_prefix: str) -> str:
    parsed = urlparse(location)
    if not parsed.netloc:
        return proxy_prefix + location
    if parsed.hostname in ["localhost", "127.0.0.1"]:
        return proxy_prefix + parsed.path
    return location


async def proxy_fetch(
    method: str, url: str, headers: dict, params: dict, body: bytes, proxy_prefix: str
):
    async with ClientSession(auto_decompress=True) as session:
        async with session.request(
            method,
            url,
            headers=headers,
            params=params,
            data=body,
            allow_redirects=False,
        ) as upstream:
            if upstream.status in (301, 302, 303, 307, 308) and upstream.headers.get(
                "Location"
            ):
                loc = upstream.headers["Location"]
                new_loc = rewrite_location(loc, proxy_prefix)
                return HTMLResponse(
                    status_code=upstream.status, headers={"Location": new_loc}
                )
            content = await upstream.read()
            media_type = upstream.headers.get("Content-Type", "text/html")
            resp_headers = {
                k: v
                for k, v in upstream.headers.items()
                if k.lower() not in ["content-length", "content-encoding"]
            }
            return HTMLResponse(
                content=content,
                status_code=upstream.status,
                headers=resp_headers,
                media_type=media_type,
            )


async def protected_proxy(
    service: str, path: str, request: Request, password: str = None
):
    service_info = SERVICES.get(service)
    if not service_info:
        raise HTTPException(status_code=404, detail="Service not found")
    if "password" in service_info and password != service_info["password"]:
        raise HTTPException(status_code=403, detail="Unauthorized access")
    base = service_info["url"]
    url = f"{base}/{path}" if path else base
    headers = {k: v for k, v in request.headers.items() if k.lower() != "host"}
    body = await request.body()
    return await proxy_fetch(
        request.method, url, headers, dict(request.query_params), body, f"/{service}"
    )


@app.api_route("/nzb/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def sabnzbd_proxy(path: str = "", request: Request = None):
    return await protected_proxy("nzb", path, request)


@app.api_route("/qbit/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def qbittorrent_proxy(path: str = "", request: Request = None):
    password = request.query_params.get("pass") or request.cookies.get("qbit_pass")
    if not password:
        raise HTTPException(status_code=403, detail="Missing password")
    response = await protected_proxy("qbit", path, request, password)
    if "pass" in request.query_params:
        response.set_cookie("qbit_pass", password)
    return response


@app.exception_handler(Exception)
async def page_not_found(_, exc):
    return HTMLResponse(
        f"<h1>404: Task not found! Mostly wrong input. <br><br>Error: {exc}</h1>",
        status_code=404,
    )
