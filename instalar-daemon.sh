#!/usr/bin/env bash
# Instala hosting público 24/7: servidor + túnel con auto-reinicio
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

LABEL="com.venezuelatebusca.daemon"
PLIST_SRC="$ROOT/com.venezuelatebusca.daemon.plist"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"
WRAPPER="$HOME/bin/venezuelatebusca-daemon.sh"

mkdir -p "$HOME/bin" "$ROOT/logs" "$ROOT/.run"
chmod +x "$ROOT/daemon-publico.sh" "$ROOT/desplegar-render.sh" "$ROOT/servidor.sh" "$ROOT/instalar-daemon.sh"

cat >"$WRAPPER" <<EOF
#!/usr/bin/env bash
ROOT="$ROOT"
cd "\$ROOT" || exit 1
exec "\$ROOT/daemon-publico.sh" run
EOF
chmod +x "$WRAPPER"

cp "$PLIST_SRC" "$PLIST_DST"
UID_NUM="$(id -u)"
DOMAIN="gui/$UID_NUM"

launchctl bootout "$DOMAIN" "$LABEL" 2>/dev/null || true
if launchctl bootstrap "$DOMAIN" "$PLIST_DST" 2>/dev/null; then
  launchctl enable "$DOMAIN/$LABEL" 2>/dev/null || true
  launchctl kickstart -k "$DOMAIN/$LABEL" 2>/dev/null || true
  echo "▸ LaunchAgent instalado (arranque al iniciar sesión)"
else
  echo "▸ LaunchAgent no disponible — iniciando daemon en background…"
  if ! pgrep -f "daemon-publico.sh run" >/dev/null 2>&1; then
    nohup "$ROOT/daemon-publico.sh" run >>"$ROOT/logs/daemon-nohup.log" 2>&1 &
  fi
fi

echo "▸ Esperando URL pública…"
for _ in $(seq 1 35); do
  if [ -f "$ROOT/PUBLIC_URL.txt" ] && [ -s "$ROOT/PUBLIC_URL.txt" ]; then
    URL="$(head -1 "$ROOT/PUBLIC_URL.txt")"
    echo ""
    echo "✓ Venezuela te Busca — URL pública:"
    echo "  $URL"
    echo ""
    echo "  Visor: $URL/static/visor.html"
    echo "  Health: $URL/health"
    exit 0
  fi
  sleep 1
done

echo "▸ Daemon en marcha; revisa PUBLIC_URL.txt en unos segundos:"
echo "  tail -f $ROOT/logs/daemon.log"