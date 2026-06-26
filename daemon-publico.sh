#!/usr/bin/env bash
# Mantiene Red de Esperanza + túnel público con auto-reinicio
set -uo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

PORT="${PORT:-8000}"
LOG_DIR="$ROOT/logs"
mkdir -p "$LOG_DIR"
URL_FILE="$ROOT/PUBLIC_URL.txt"
PID_DIR="$ROOT/.run"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_DIR/daemon.log"; }

start_server() {
  if curl -sf "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
    return 0
  fi
  log "Iniciando Red de Esperanza en puerto $PORT…"
  lsof -ti:"$PORT" 2>/dev/null | xargs kill -9 2>/dev/null || true
  nohup "$ROOT/servidor.sh" >>"$LOG_DIR/server.log" 2>&1 &
  echo $! >"$PID_DIR/server.pid"
  for _ in $(seq 1 30); do
    if curl -sf "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
      log "Servidor OK"
      return 0
    fi
    sleep 1
  done
  log "ERROR: servidor no respondió"
  return 1
}

start_tunnel() {
  local cf="$ROOT/cloudflared"
  if [ ! -x "$cf" ]; then
    curl -fsSL "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-amd64.tgz" \
      -o /tmp/cloudflared.tgz
    tar -xzf /tmp/cloudflared.tgz -C "$ROOT" cloudflared
    chmod +x "$cf"
  fi

  if [ -f "$PID_DIR/tunnel.pid" ] && kill -0 "$(cat "$PID_DIR/tunnel.pid")" 2>/dev/null; then
    return 0
  fi

  log "Iniciando túnel público…"
  : >"$LOG_DIR/tunnel.log"
  nohup "$cf" tunnel --url "http://127.0.0.1:${PORT}" >>"$LOG_DIR/tunnel.log" 2>&1 &
  echo $! >"$PID_DIR/tunnel.pid"

  for _ in $(seq 1 25); do
    local url
    url=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOG_DIR/tunnel.log" 2>/dev/null | head -1)
    if [ -n "$url" ]; then
      echo "$url" >"$URL_FILE"
      echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $url" >>"$LOG_DIR/urls.log"
      log "URL pública: $url"
      return 0
    fi
    sleep 1
  done
  log "ERROR: no se obtuvo URL del túnel"
  return 1
}

watch_loop() {
  log "Daemon Red de Esperanza activo"
  while true; do
    start_server || true
    if ! start_tunnel; then
      rm -f "$PID_DIR/tunnel.pid"
      sleep 5
    fi
    if [ -f "$PID_DIR/tunnel.pid" ] && ! kill -0 "$(cat "$PID_DIR/tunnel.pid")" 2>/dev/null; then
      log "Túnel caído — reiniciando…"
      rm -f "$PID_DIR/tunnel.pid"
      start_tunnel || true
    fi
    if ! curl -sf "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
      log "Servidor caído — reiniciando…"
      rm -f "$PID_DIR/server.pid"
      start_server || true
    fi
    sleep 20
  done
}

mkdir -p "$PID_DIR"
case "${1:-run}" in
  start) start_server && start_tunnel ;;
  run) watch_loop ;;
  url) cat "$URL_FILE" 2>/dev/null || echo "Sin URL aún" ;;
  stop)
    kill "$(cat "$PID_DIR/server.pid" 2>/dev/null)" 2>/dev/null || true
    kill "$(cat "$PID_DIR/tunnel.pid" 2>/dev/null)" 2>/dev/null || true
    rm -f "$PID_DIR"/*.pid
    log "Daemon detenido"
    ;;
  *) echo "Uso: $0 {run|start|url|stop}" ;;
esac