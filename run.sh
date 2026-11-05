#!/usr/bin/env bash
# Trade Republic dashboard — start / stop / status.
#   ./run.sh            start (web + sync worker) and open the browser
#   ./run.sh stop       stop
#   ./run.sh restart
#   ./run.sh status
#   ./run.sh sync       one foreground sync (approve the login in the TR app)
set -euo pipefail
cd "$(dirname "$0")"

PORT="${TRDASH_PORT:-8085}"
DATA="${TRDASH_DATA_DIR:-$PWD/data}"
PID="$DATA/trdash.pid"
LOG="$DATA/logs/server.log"
mkdir -p "$DATA/logs"

command -v uv >/dev/null || { echo "uv is required: https://docs.astral.sh/uv/"; exit 1; }

setup() {
  uv sync --quiet
  # Chromium for tr-api's WAF token (one-time, ~150 MB, cached in ~/.cache).
  uv run --quiet playwright install chromium >/dev/null 2>&1 || true
  uv run --quiet python manage.py migrate --no-input -v0
}

running() { [[ -f "$PID" ]] && kill -0 "$(cat "$PID")" 2>/dev/null; }

start() {
  if running; then echo "Already running → http://localhost:$PORT/"; return; fi
  setup
  nohup uv run python manage.py serve --port "$PORT" >>"$LOG" 2>&1 &
  echo $! >"$PID"
  for _ in $(seq 30); do
    curl -fs -o /dev/null "http://localhost:$PORT/" && break
    sleep 0.5
  done
  echo "Running → http://localhost:$PORT/  (log: $LOG)"
  if command -v xdg-open >/dev/null; then xdg-open "http://localhost:$PORT/" >/dev/null 2>&1 || true
  elif command -v open >/dev/null; then open "http://localhost:$PORT/" || true; fi
}

stop() {
  if running; then
    pkill -TERM -P "$(cat "$PID")" 2>/dev/null || true
    kill "$(cat "$PID")" 2>/dev/null || true
    echo "Stopped."
  else
    echo "Not running."
  fi
  rm -f "$PID"
}

case "${1:-start}" in
  start) start ;;
  stop) stop ;;
  restart) stop; sleep 1; start ;;
  status) if running; then echo "Running (pid $(cat "$PID")) → http://localhost:$PORT/"; else echo "Stopped."; fi ;;
  sync) setup; uv run python manage.py sync "${@:2}" ;;
  *) sed -n '2,8p' "$0"; exit 1 ;;
esac
