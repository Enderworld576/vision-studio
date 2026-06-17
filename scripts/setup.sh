#!/usr/bin/env bash
# One-time setup: create the Python environment and install everything.
# Usage:  ./scripts/setup.sh   (run from the vision-studio folder or anywhere)
set -e
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "==> Vision Studio setup"
PYBIN="${PYTHON:-python3}"
echo "    using $($PYBIN --version)"

# 1) Python virtual environment (handles the case where ensurepip is missing).
if [ ! -x ".venv/bin/python" ]; then
  echo "==> creating .venv"
  if "$PYBIN" -m venv .venv 2>/dev/null && [ -x ".venv/bin/pip" ]; then :; else
    echo "    (bootstrapping pip without ensurepip)"
    "$PYBIN" -m venv --without-pip .venv
    curl -fsSL https://bootstrap.pypa.io/get-pip.py | .venv/bin/python
  fi
fi
.venv/bin/python -m pip install --upgrade pip

# 2) Python dependencies. Try CPU-only torch first to keep the download small;
#    fall back to the default wheel if a matching CPU wheel isn't available.
echo "==> installing Python packages (this downloads PyTorch — a few minutes)"
.venv/bin/python -m pip install torch torchvision \
  --index-url https://download.pytorch.org/whl/cpu 2>/dev/null || \
  echo "    (CPU-only torch unavailable for this Python; using default wheel)"
.venv/bin/python -m pip install -r backend/requirements.txt

# 3) Electron (optional — only needed for the desktop window).
if command -v npm >/dev/null 2>&1; then
  echo "==> installing Electron"
  npm install
else
  echo "    npm not found — skipping Electron. You can still run the web app:"
  echo "    .venv/bin/python backend/server.py  then open http://127.0.0.1:8600"
fi

echo "==> done.  Start it with:  ./scripts/run.sh"
