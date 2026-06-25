#!/usr/bin/env bash
# Despliegue en Render.com vía API (requiere RENDER_API_KEY en .env)
set -euo pipefail
cd "$(dirname "$0")"

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

API_KEY="${RENDER_API_KEY:-}"
OWNER_ID="${RENDER_OWNER_ID:-}"

if [ -z "$API_KEY" ] || [ -z "$OWNER_ID" ]; then
  echo "Sin RENDER_API_KEY / RENDER_OWNER_ID — iniciando hosting público local…"
  exec ./daemon-publico.sh run
fi

echo "▸ Desplegando blueprint en Render…"
curl -fsS -X POST "https://api.render.com/v1/blueprint-instances" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d "{
    \"repo\": \"https://github.com/jomstudiovzla/VENEZUELATEBUSCA\",
    \"branch\": \"main\"
  }" | python3 -m json.tool

echo "▸ Revisa https://dashboard.render.com para la URL del servicio"