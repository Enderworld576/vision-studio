# Roadmap

## Done
- Unified Python backend (camera + dataset + training + detection + export)
  behind one HTTP API; polished web UI (Collect, Train, Test); Electron shell.
- **General-purpose & part-agnostic**: trains a detector for whatever you label;
  multi-instance detection (de-duplicated); orientation = shape axis + learned
  appearance-template + ORB features (no per-part code).
- **Model export**: runnable bundle (.pt + ONNX + classes + orientation + the
  actual inference code + README).
- **Multi-class**: one model can detect several labeled classes at once
  ("★ All parts" in Train); inference returns per-class results; the UI shows
  class names + colors. Orientation is an optional per-class add-on (per-class
  references under `<model>.orient/`).
- **Effortless install (in progress)**: `electron-builder` config for
  AppImage/dmg/exe + bundled Python (`scripts/bundle-python.sh`, CPU-only torch)
  + a **first-run setup screen** (`electron/setup.js`, `renderer/setup.html`)
  that verifies/installs deps, downloads base weights with progress, and reports
  errors. Packaged apps store data under the OS user-data dir. Dev path
  (`setup.sh`) preserved. **Remaining:** run `bundle-python.sh` + `npm run dist`
  per OS in CI, plus macOS notarization / Windows signing. See `docs/PACKAGING.md`.

## Next
1. **Produce + smoke-test the actual installers** (AppImage first) in CI.
2. **First-run tour** — a short guided overlay after setup.
3. **Collect UX polish** — multi-box per image, edit/redraw boxes, shortcuts.
4. **Training quality** — show val predictions; warn on too-few images; pick
   model size (n/s/m).

## Known limitations
- CPU training only (fine for these dataset sizes; minutes, not seconds).
- Installers built for Linux (AppImage). macOS/Windows need their native runners
  (`bundle-python.sh` + `npm run dist`) + signing — see `docs/PACKAGING.md`.
