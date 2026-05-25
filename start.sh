#!/usr/bin/env bash
set -e

cd "$(dirname "$0")"
source .venv/bin/activate
export PYTHONPATH="${PWD}:${PYTHONPATH}"

python3 update.py
python3 -m bot
