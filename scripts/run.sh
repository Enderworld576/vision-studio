#!/usr/bin/env bash
# Launch Vision Studio.
#   ./scripts/run.sh         desktop app (Electron window)
#   ./scripts/run.sh --web   web only (no Electron) — opens in your browser
set -e
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [ ! -x ".venv/bin/python" ]; then
  echo "Not set up yet. Run ./scripts/setup.sh first."; exit 1
fi

if [ "$1" = "--web" ] || ! command -v npm >/dev/null 2>&1 || [ ! -d node_modules ]; then
  PORT="${VS_PORT:-8600}"
  echo "Starting web app on http://127.0.0.1:$PORT  (Ctrl-C to stop)"
  ( sleep 2; (command -v xdg-open >/dev/null && xdg-open "http://127.0.0.1:$PORT") || true ) &
  exec .venv/bin/python backend/server.py --port "$PORT"
else
  exec npm start
fi
