#!/usr/bin/env bash
# Arranque en producción (Render, Railway, VPS, Docker)
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8000}"
HOST="${HOST:-0.0.0.0}"
WORKERS="${WEB_CONCURRENCY:-1}"

if [ -d /data ] && [ -w /data ]; then
  export DATABASE_URL="${DATABASE_URL:-sqlite+aiosqlite:////data/red_esperanza.db}"
  if [ ! -e /data/red_esperanza.db ] && [ -e ./red_esperanza.db ]; then
    echo "▸ Copiando red_esperanza.db → /data/"
    cp -a ./red_esperanza.db /data/ 2>/dev/null || true
  fi
  if [ -f /data/red_esperanza.db ]; then
    ln -sfn /data/red_esperanza.db ./red_esperanza.db 2>/dev/null || true
  fi
fi

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
source .venv/bin/activate

if ! python3 -c "import fastapi" 2>/dev/null; then
  pip install -q --upgrade pip
  pip install -q -r requirements-core.txt
fi

if [ ! -f .env ] && [ -f .env.example ]; then
  cp .env.example .env
fi

echo "▸ Red de Esperanza — producción"
echo "  Host: $HOST  Puerto: $PORT  Workers: $WORKERS"

exec uvicorn main:app \
  --host "$HOST" \
  --port "$PORT" \
  --workers "$WORKERS" \
  --proxy-headers \
  --forwarded-allow-ips='*'