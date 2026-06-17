# Architecture

One Python backend does everything; a web UI talks to it; Electron is a thin
window around that UI.

```
┌─────────────────────────────────────────────┐
│ Electron window  (electron/main.js)          │  ← optional desktop shell
│   loads  http://127.0.0.1:8600               │
└───────────────────────┬─────────────────────┘
                        │ HTTP (same origin)
┌───────────────────────▼─────────────────────┐
│ Web UI            (renderer/)                │  index.html · app.js · styles.css
│   Home · Collect · Train · Test              │
└───────────────────────┬─────────────────────┘
                        │ /api/*  (JSON + MJPEG)
┌───────────────────────▼─────────────────────┐
│ Backend           (backend/server.py, Flask) │
│   camera.py    OAK 4 / USB / IP cameras       │
│   dataset.py   YOLO dataset CRUD              │
│   training.py  YOLO11 training (threaded)     │
│   vision.py    detect + orientation           │
│   orientation.py  pose estimation             │
└───────────────────────┬─────────────────────┘
                        │
              data/  (dataset, models, config.json)
```

## Why one backend process

DepthAI (camera) and Ultralytics/torch (ML) are verified to coexist in a single
CPython 3.14 environment, so there's no separate camera service to manage — the
detector reads frames straight from the in-process `Camera`. This is the main
simplification over the earlier setup (Electron IPC + a separate frame server +
standalone `ml/` scripts).

## Backend API (all under `/api`)

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/health` | model + camera status |
| GET/POST | `/config` | settings (device, fps, zero_deg, active_model) |
| GET | `/camera/status` | camera state |
| GET | `/cameras` | discover OAK / USB / IP cameras + active |
| POST | `/camera/select` | switch camera (`kind`: oak/usb/ip, `source`) |
| POST | `/camera/start` `/camera/stop` | (re)connect / release camera |
| GET | `/camera/stream` | live MJPEG |
| GET | `/camera/snapshot` | one JPEG still |
| GET | `/dataset/stats` | counts per class |
| GET/POST | `/classes` | list / add a class |
| GET/POST | `/samples` | list / save a labeled sample |
| GET | `/samples/<stem>/image` | a sample's image |
| DELETE | `/samples/<stem>` | delete a sample |
| POST | `/train` · GET `/train/status` | start training / poll progress |
| GET | `/models` | available + active model |
| POST | `/detect` | detect (source: `camera` or `image`+`data`) |
| POST | `/calibrate` | set current pose as 0° |

## Lineage (where this came from)

- `camera.py` ← `tools/data-collector/python/frame_server.py`
- `dataset.py` ← the data-collector's Electron IPC dataset handlers
- `training.py` ← `ml/train.py` + `ml/prepare_dataset.py`
- `vision.py` / `orientation.py` ← `ml/detect.py` + `ml/orientation.py`
- UI ← `ml/app.py` (tester) + the data-collector renderer, unified

## Orientation

The detector gives an axis-aligned box (position). Orientation is computed
classically in `orientation.py`: GrabCut segments the assembly, its min-area
rectangle gives the long axis, and the white carrier's overhang past the PCB
picks the "front" (blank-tab) direction. See `ml/README.md` for the full
rationale and tuning knobs.
