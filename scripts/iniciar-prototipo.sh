#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

LAN_IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}' || echo "127.0.0.1")"
PORT="${PORT:-8000}"
URL="http://${LAN_IP}:${PORT}/mobile/"
INSTALL="http://${LAN_IP}:${PORT}/mobile/install.html"

echo ""
echo "  ╔══════════════════════════════════════════════╗"
echo "  ║     Red de Esperanza — Prototipo móvil       ║"
echo "  ╚══════════════════════════════════════════════╝"
echo ""
echo "  iPhone / iPad (Safari, misma Wi‑Fi):"
echo "  → ${URL}"
echo ""
echo "  QR e instrucciones:"
echo "  → ${INSTALL}"
echo ""
echo "  Login: JOM / Studio"
echo ""
echo "  Ctrl+C para detener"
echo ""

if command -v open >/dev/null 2>&1; then
  open "${INSTALL}" 2>/dev/null || true
fi

exec python3 -m uvicorn main:app --host 0.0.0.0 --port "$PORT" --reload