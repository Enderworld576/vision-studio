# Packaging Vision Studio into downloadable installers

Goal: a non-technical user downloads one file, opens it, and the app works —
no terminal, no Python install. We achieve that by **bundling a self-contained
Python runtime** inside the Electron app and running a **one-time first-run
setup** for anything that must be fetched at install time (base model weights).

## How it fits together
- **Electron shell** (`electron/main.js`) launches the **Python/Flask backend**
  and shows its UI. It resolves the Python executable from `electron/setup.js`
  → `pythonPath()`:
  - **packaged:** `resources/pyenv/bin/python` (the bundled runtime),
  - **dev:** the project `.venv`.
- **First-run setup** (`electron/setup.js` + `renderer/setup.html`): on first
  launch (or if dependencies are missing) a setup window verifies the engine,
  installs anything missing, downloads the base detector weights with live
  progress, and reports errors clearly. A marker file
  (`<userData>/data/.setup-done`) records completion so it only runs once.
- **Writable data:** packaged resources are read-only, so the backend stores the
  dataset/models/config under the OS user-data dir (`app.getPath('userData')/
  data`); in dev it uses the project `data/` folder.
- `asar: false` — the Python backend (a separate process) must read its own
  files from disk, so the app isn't packed into an asar archive.

## Build steps (run on each target OS — a Python env can't be cross-built)
```bash
npm install
./scripts/bundle-python.sh   # downloads a standalone Python + CPU-only PyTorch into ./pyenv,
                             # installs backend/requirements.txt + onnx, pre-fetches yolo11n.pt
npm run dist                 # electron-builder -> installers/  (AppImage / dmg / exe)
```
`npm run pack` makes an unpacked build (faster, for testing) in `installers/`.

## Keeping the download small
`bundle-python.sh` installs **CPU-only PyTorch** (`--index-url
https://download.pytorch.org/whl/cpu`), which is far smaller than the default
CUDA build. Expect roughly a few-hundred-MB installer.

## CI — build all three on GitHub Actions (recommended)
`.github/workflows/build-installers.yml` builds Linux/macOS/Windows installers
on their native runners and attaches them to a GitHub Release. Two ways to run:

- **Tag a release:** `git tag v0.1.0 && git push origin v0.1.0` → the workflow
  builds all three and uploads them to the `v0.1.0` release.
- **Attach to an existing release:** Actions tab → *Build installers* → *Run
  workflow* → enter the tag (e.g. `v0.1.0`). Useful when the Linux build was
  uploaded by hand and you just need macOS/Windows added.

Each job runs `bundle-python.sh` then `electron-builder --publish never`, so the
standalone-Python version can be pinned with `PBS_TAG` / `PBS_URL` / `PY_V`.

Caveats for the hosted runners:
- **macOS** (`macos-latest`) is Apple Silicon, so the `.dmg` is **arm64-only**;
  Intel Macs need a separate `macos-13` job.
- The build is **unsigned** (`CSC_IDENTITY_AUTO_DISCOVERY=false`); see the
  signing notes below to silence Gatekeeper/SmartScreen.
- **Private-repo Actions minutes** are metered (macOS counts 10×) — a full
  three-OS build can use a few hundred minutes' worth.

## Notes / gotchas
- macOS distribution outside the App Store needs code-signing + notarization to
  avoid Gatekeeper warnings (add `mac.identity` / notarize config).
- Windows SmartScreen is friendlier with a signed `.exe` (add a cert).
- If `bundle-python.sh`'s download URL 404s, the standalone-Python release tag
  has moved — set `PBS_TAG`/`PBS_URL` to a current one.
