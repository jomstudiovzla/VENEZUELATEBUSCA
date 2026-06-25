#!/usr/bin/env bash
# Expone tu localhost al internet (URL pública temporal, sin cuenta)
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8000}"

if ! curl -sf "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
  echo "▸ El servidor no está corriendo. Iniciando…"
  ./arrancar.sh &
  sleep 4
fi

CLOUDFLARED="$(command -v cloudflared 2>/dev/null || true)"
[ -z "$CLOUDFLARED" ] && [ -x "./cloudflared" ] && CLOUDFLARED="./cloudflared"

if [ -n "$CLOUDFLARED" ]; then
  echo ""
  echo "▸ URL pública (Cloudflare Tunnel) — comparte el enlace *.trycloudflare.com"
  echo "  Presiona Ctrl+C para cerrar el túnel."
  echo ""
  exec "$CLOUDFLARED" tunnel --url "http://127.0.0.1:${PORT}"
fi

if command -v ngrok >/dev/null 2>&1; then
  echo "▸ URL pública (ngrok)"
  exec ngrok http "$PORT"
fi

echo "Instalando cloudflared…"
if command -v brew >/dev/null 2>&1; then
  brew install cloudflared
  exec cloudflared tunnel --url "http://127.0.0.1:${PORT}"
fi

echo "No se encontró cloudflared ni ngrok."
echo "Opciones:"
echo "  1) brew install cloudflared && ./exponer.sh"
echo "  2) Desplegar en Render: ver README sección Despliegue"
exit 1