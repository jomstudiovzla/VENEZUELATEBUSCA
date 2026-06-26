#!/usr/bin/env bash
# Instalación rápida — Red de Esperanza
set -euo pipefail
cd "$(dirname "$0")"

echo "▸ Creando entorno virtual…"
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
source .venv/bin/activate

echo "▸ Instalando dependencias (logística humanitaria)…"
pip install -q --upgrade pip
pip install -q -r requirements-core.txt

if [ ! -f .env ]; then
  cp .env.example .env
  echo "▸ Creado .env desde .env.example"
fi

echo ""
echo "✓ Instalación lista."
echo "  Arrancar:   ./arrancar.sh"
echo "  Dashboard:  http://127.0.0.1:8000/"
echo "  Público:    ./exponer.sh"
echo ""