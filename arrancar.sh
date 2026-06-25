#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
source .venv/bin/activate
pip install -q -r requirements.txt
if [ ! -f .env ]; then
  cp .env.example .env
fi

echo "Venezuela te Busca — http://127.0.0.1:8000/"
exec uvicorn main:app --host 0.0.0.0 --port 8000