import re

from aiohttp import ClientSession, ClientTimeout

from ... import LOGGER
from ...core.config_manager import Config

TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/original"
TMDB_WEB_BASE = "https://www.themoviedb.org"
COMMON_TAGS = {
    "480p",
    "720p",
    "1080p",
    "1440p",
    "2160p",
    "4320p",
    "4k",
    "8k",
    "webrip",
    "web-dl",
    "webdl",
    "bluray",
    "brrip",
    "bdrip",
    "dvdrip",
    "hdrip",
    "remux",
    "uhd",
    "hdr",
    "dv",
    "hevc",
    "x264",
    "x265",
    "h264",
    "h265",
    "av1",
    "aac",
    "ddp",
    "dd",
    "dts",
    "truehd",
    "atmos",
    "10bit",
    "8bit",
    "multi",
    "proper",
    "repack",
    "extended",
    "uncut",
    "dubbed",
    "dual",
    "audio",
    "esubs",
    "subs",
    "subbed",
    "complete",
    "nf",
    "amzn",
    "dsnp",
    "hmax",
    "web",
    "hdcam",
    "cam",
    "hc",
    "readnfo",
    "imax",
    "hindi",
    "english",
    "japanese",
    "korean",
}
SEASON_EPISODE_RE = r"\bS\d{1,2}E\d{1,2}\b|\bS\d{1,2}\b|\bE\d{1,3}\b"
YEAR_RE = r"\b(?:19|20)\d{2}\b"
STOP_TOKENS = rf"(?:{YEAR_RE}|{SEASON_EPISODE_RE}|\b(?:{'|'.join(sorted(COMMON_TAGS))})\b)"


def extract_search_query(name: str) -> tuple[str, str | None]:
    base_name = name.rsplit("/", 1)[-1]
    base_name = re.sub(r"\.[A-Za-z0-9]{2,4}$", "", base_name)
    normalized = re.sub(r"[._\-\[\]\(\)]+", " ", base_name)
    match = re.search(YEAR_RE, normalized)
    year = match.group(0) if match else None
    title_part = re.split(STOP_TOKENS, normalized, maxsplit=1, flags=re.IGNORECASE)[0]
    title_part = re.sub(r"\s+", " ", title_part).strip()
    if len(title_part) < 2:
        title_part = re.sub(YEAR_RE, "", normalized).strip() or normalized.strip()
    return title_part, year


async def fetch_tmdb_poster(name: str) -> dict | None:
    api_key = Config.get("TMDB_API_KEY")
    if not api_key or not name:
        return None

    query, year = extract_search_query(name)
    if not query:
        return None

    params = {"api_key": api_key, "query": query, "include_adult": "true"}
    timeout = ClientTimeout(total=10)
    try:
        async with ClientSession(timeout=timeout) as session:
            async with session.get(
                "https://api.themoviedb.org/3/search/multi", params=params
            ) as response:
                if response.status != 200:
                    LOGGER.warning(
                        "TMDb lookup failed for %s with status %s", query, response.status
                    )
                    return None
                data = await response.json()
    except Exception:
        LOGGER.exception("TMDb lookup error for %s", query)
        return None

    results = [
        item
        for item in data.get("results", [])
        if item.get("media_type") in {"movie", "tv"} and item.get("poster_path")
    ]
    if not results:
        return None

    def score(item: dict) -> tuple[int, float, str]:
        item_date = item.get("release_date") or item.get("first_air_date") or ""
        item_year = item_date[:4] if item_date else ""
        exact_year = int(bool(year and item_year == year))
        popularity = float(item.get("popularity") or 0)
        return exact_year, popularity, item_date

    best = sorted(results, key=score, reverse=True)[0]
    media_type = best["media_type"]
    title = best.get("title") or best.get("name") or query
    return {
        "title": title,
        "poster": f"{TMDB_IMAGE_BASE}{best['poster_path']}",
        "url": f"{TMDB_WEB_BASE}/{'movie' if media_type == 'movie' else 'tv'}/{best['id']}",
        "year": (best.get("release_date") or best.get("first_air_date") or "")[:4]
        or year,
    }
