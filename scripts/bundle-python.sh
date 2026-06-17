#!/usr/bin/env bash
# Prepare a relocatable, self-contained Python runtime in ./pyenv for packaging.
# electron-builder bundles ./pyenv as extraResources, so the shipped app needs
# no system Python and no setup. Run this ONCE per target OS before `npm run
# dist` (you cannot cross-build a Python env — build on each platform / in CI).
#
#   ./scripts/bundle-python.sh        # then: npm run dist
#
# Uses python-build-standalone + CPU-only PyTorch to keep the download small.
set -e
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# --- pick a python-build-standalone asset for this platform ---------------
# Override PBS_URL to use a newer release: https://github.com/astral-sh/python-build-standalone/releases
PBS_TAG="${PBS_TAG:-20240814}"
PY_V="${PY_V:-3.11.9}"
OS="$(uname -s)"; ARCH="$(uname -m)"
case "$OS-$ARCH" in
  Linux-x86_64)   TRIPLE="x86_64-unknown-linux-gnu" ;;
  Linux-aarch64)  TRIPLE="aarch64-unknown-linux-gnu" ;;
  Darwin-arm64)   TRIPLE="aarch64-apple-darwin" ;;
  Darwin-x86_64)  TRIPLE="x86_64-apple-darwin" ;;
  *) echo "Unsupported platform $OS-$ARCH — set PBS_URL manually."; exit 1 ;;
esac
PBS_URL="${PBS_URL:-https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_TAG}/cpython-${PY_V}+${PBS_TAG}-${TRIPLE}-install_only.tar.gz}"

echo "==> fetching standalone Python: $PBS_URL"
rm -rf pyenv pyenv.tmp && mkdir pyenv.tmp
curl -fL "$PBS_URL" -o pyenv.tmp/python.tar.gz
tar -xzf pyenv.tmp/python.tar.gz -C pyenv.tmp     # extracts to pyenv.tmp/python
mv pyenv.tmp/python pyenv
rm -rf pyenv.tmp
PY="pyenv/bin/python3"

echo "==> installing CPU-only PyTorch + dependencies"
"$PY" -m pip install --upgrade pip
"$PY" -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu \
  || { echo "(CPU wheel unavailable for this Python; using default torch)"; "$PY" -m pip install torch torchvision; }
"$PY" -m pip install -r backend/requirements.txt onnx

echo "==> pre-downloading base detector weights"
"$PY" -c "from ultralytics import YOLO; YOLO('yolo11n.pt')" || true

echo "==> done. pyenv/ is ready. Build installers with:  npm run dist"
