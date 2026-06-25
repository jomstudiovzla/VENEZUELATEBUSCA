#!/usr/bin/env bash
# Arranque en producción (Render, Railway, VPS, Docker)
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8000}"
HOST="${HOST:-0.0.0.0}"
WORKERS="${WEB_CONCURRENCY:-1}"

if [ -d /data ] && [ -w /data ]; then
  export DATABASE_URL="${DATABASE_URL:-sqlite+aiosqlite:////data/ojo_de_dios.db}"
  for item in ojo_de_dios.db reference_photos building_photos; do
    if [ ! -e "/data/$item" ] && [ -e "./$item" ]; then
      echo "▸ Copiando $item → /data/"
      cp -a "./$item" "/data/" 2>/dev/null || true
    fi
  done
  mkdir -p /data/reference_photos /data/building_photos
  ln -sfn /data/reference_photos ./reference_photos 2>/dev/null || true
  ln -sfn /data/building_photos ./building_photos 2>/dev/null || true
  if [ -f /data/ojo_de_dios.db ]; then
    ln -sfn /data/ojo_de_dios.db ./ojo_de_dios.db 2>/dev/null || true
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

echo "▸ Venezuela te Busca — producción"
echo "  Host: $HOST  Puerto: $PORT  Workers: $WORKERS"
echo "  Tiempo real: scraper=${SCRAPER_POLL_INTERVAL:-20}s terremoto=${TERREMOTO_POLL_INTERVAL:-20}s"

exec uvicorn main:app \
  --host "$HOST" \
  --port "$PORT" \
  --workers "$WORKERS" \
  --proxy-headers \
  --forwarded-allow-ips='*'