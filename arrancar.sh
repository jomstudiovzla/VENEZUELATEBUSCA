#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  chmod +x instalar.sh 2>/dev/null || true
  ./instalar.sh
fi
source .venv/bin/activate

if ! python3 -c "import fastapi" 2>/dev/null; then
  pip install -q -r requirements-core.txt
fi
if [ ! -f .env ]; then
  cp .env.example .env
fi

echo "Venezuela te Busca — http://127.0.0.1:8000/"
exec uvicorn main:app --host 0.0.0.0 --port 8000