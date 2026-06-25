#!/usr/bin/env bash
# Instalación rápida — Venezuela te Busca
set -euo pipefail
cd "$(dirname "$0")"

echo "▸ Creando entorno virtual…"
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
source .venv/bin/activate

echo "▸ Instalando dependencias base (visor + API + reportes)…"
pip install -q --upgrade pip
pip install -q -r requirements-core.txt

if [ ! -f .env ]; then
  cp .env.example .env
  echo "▸ Creado .env desde .env.example"
fi

if [ ! -f ojo_de_dios.db ]; then
  echo "⚠  No se encontró ojo_de_dios.db — el visor arrancará vacío hasta sincronizar."
fi

echo ""
echo "✓ Instalación lista."
echo "  Arrancar:  ./arrancar.sh"
echo "  Visor:     http://127.0.0.1:8000/"
echo ""
echo "  (Opcional) ML avanzado para feeds SAR:"
echo "  pip install torch torchvision && pip install -r requirements.txt"