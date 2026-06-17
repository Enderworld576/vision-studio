#!/usr/bin/env python3
"""Vision Studio backend — one HTTP service for camera, dataset, training,
and detection. Serves the web UI (../renderer) and a JSON/MJPEG API.

    python server.py [--port 8600] [--data-dir ../data] [--device 169.254.1.222]

Everything the UI needs is under /api/*. State (config, dataset, models) lives
in the data dir, so the app is fully self-contained.
"""

import argparse
import base64
import json
import math
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

import cv2
import numpy as np
from flask import Flask, Response, jsonify, request, send_from_directory

from camera import make_camera, discover_cameras
from dataset import Dataset
from training import Trainer
from vision import Vision

HERE = Path(__file__).resolve().parent
RENDERER = HERE.parent / "renderer"


class Config:
    """Tiny JSON-backed settings store."""
    DEFAULTS = {"device": "169.254.1.222", "fps": 15, "zero_deg": 0.0,
                "target_class": "part", "active_model": None,
                "camera_kind": "oak", "camera_source": None}  # source None -> device (OAK IP)

    def __init__(self, path):
        self.path = Path(path)
        self.data = dict(self.DEFAULTS)
        if self.path.exists():
            self.data.update(json.loads(self.path.read_text()))

    def get(self, k):
        return self.data.get(k, self.DEFAULTS.get(k))

    def update(self, **kw):
        self.data.update({k: v for k, v in kw.items() if v is not None})
        self.path.write_text(json.dumps(self.data, indent=2) + "\n")
        return self.data


def _readme_md(names, onnx_ok, has_orient):
    classes = ", ".join(f"{i}: {n}" for i, n in enumerate(names))
    onnx_note = ("`model.onnx` — universal, runs via onnxruntime in any language."
                 if onnx_ok else "ONNX export failed (see ONNX_EXPORT_FAILED.txt); use model.pt.")
    orient_block = (f"""
## Orientation (the custom part)

`orientation.npz` is a reference of the part's appearance, learned from the
front/back labels. The facing angle of each detection is computed *after* the
box, in two stages (this is what `infer.py` does for you):

1. **Axis** — segment the part from the box (`orientation.shape_axis`, GrabCut +
   min-area rectangle) to get the orientation *line*.
2. **Facing** — read which way it points from internal features
   (`orient_template.orient`): a CLAHE appearance-template correlation, plus ORB
   feature matching, against the learned reference. Returns a directed angle.

All of this is plain OpenCV/NumPy in `orientation.py` and `orient_template.py`
(no black box) — read or adapt them freely.
""" if has_orient else """
## Orientation
This model was exported without an orientation reference, so `infer.py` returns
the detection boxes (and the undirected shape axis when the silhouette is clear).
""")
    return f"""# Vision Studio — exported model

A standalone **part detector** (+ orientation) you can run as-is or drop into
your own programs. Detects every instance of these classes: {classes}

## Quick start
```bash
pip install -r requirements.txt
python example.py your_image.jpg        # prints detections + writes annotated.jpg
```

## Use it in your code
```python
import cv2
from infer import VisionStudio
vs = VisionStudio("model.pt", "orientation.npz")   # 2nd arg optional
for d in vs.detect(cv2.imread("your_image.jpg")):
    # every instance of the part, de-duplicated
    print(d["conf"], d["center"], d["angle_deg"], d["method"], d["bbox"])
```

## What's in this bundle
| file | what it is |
| --- | --- |
| `model.pt` | YOLO detector weights (Ultralytics / PyTorch) |
| `model.onnx` | {onnx_note} |
| `classes.txt` | class names (index order matches the model) |
| `infer.py` | **the full pipeline** — detection + de-dup + orientation (`VisionStudio`) |
| `orientation.py`, `orient_template.py` | the custom orientation code (OpenCV/NumPy) |
| `orient/` (per class) or `orientation.npz`, `orientation.json` | learned orientation reference(s) + metadata |
| `example.py` | runnable demo |
| `requirements.txt` | Python dependencies |

## Just the detector (no orientation), any language
`model.onnx` takes a 640x640 RGB image (NCHW, normalized /255). Output is
`[1, 4+num_classes, 8400]` (cx,cy,w,h + class scores) — apply a confidence
threshold + NMS. In Python, `YOLO("model.onnx")` (Ultralytics) does this for you;
for C++/C#/JS use your runtime's standard YOLO post-processing.
{orient_block}"""


def create_app(data_dir, device=None, start_camera=True):
    data = Path(data_dir)
    data.mkdir(parents=True, exist_ok=True)
    cfg = Config(data / "config.json")
    if device:
        cfg.update(device=device)

    ds = Dataset(data / "dataset")
    models_dir = data / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    trainer = Trainer(ds, models_dir, data / "work")

    def active_weights():
        m = cfg.get("active_model")
        if m and (models_dir / m).exists():
            return models_dir / m
        # else first available model
        found = sorted(models_dir.glob("*.pt"))
        return found[0] if found else models_dir / f"{cfg.get('target_class')}.pt"

    state = {"vision": Vision(active_weights(), cfg.get("target_class"))}
    last = {"img": None, "result": None}   # most recent detection, for corrections

    def build_camera():
        kind = cfg.get("camera_kind") or "oak"
        source = cfg.get("camera_source")
        if source is None and kind == "oak":
            source = cfg.get("device")
        return make_camera(kind, source, fps=int(cfg.get("fps")))

    cam = build_camera()
    if start_camera:
        cam.start()

    app = Flask(__name__, static_folder=None)

    # -- UI -----------------------------------------------------------------
    @app.get("/")
    def index():
        return send_from_directory(RENDERER, "index.html")

    @app.get("/<path:fn>")
    def static_files(fn):
        if (RENDERER / fn).exists():
            return send_from_directory(RENDERER, fn)
        return ("not found", 404)

    # -- health / config ----------------------------------------------------
    @app.get("/api/health")
    def health():
        return jsonify(ok=True, model_ready=state["vision"].ready,
                       model=str(active_weights().name),
                       camera=cam.status())

    @app.get("/api/config")
    def get_config():
        return jsonify(cfg.data)

    @app.post("/api/config")
    def set_config():
        body = request.get_json(force=True, silent=True) or {}
        cfg.update(**body)
        # apply model change live
        state["vision"] = Vision(active_weights(), cfg.get("target_class"))
        return jsonify(cfg.data)

    # -- GPU (auto-used if present; CUDA build is an opt-in install) ---------
    gpu = {"state": "idle", "log": ""}

    @app.get("/api/gpu")
    def gpu_status():
        try:
            import torch
            avail = bool(torch.cuda.is_available())
            name = torch.cuda.get_device_name(0) if avail else None
            cuda_build = torch.version.cuda is not None
        except Exception:
            avail, name, cuda_build = False, None, False
        return jsonify(available=avail, name=name, cuda_build=cuda_build,
                       using="GPU" if avail else "CPU", install_state=gpu["state"])

    @app.post("/api/gpu/install")
    def gpu_install():
        """Opt-in: install the CUDA build of PyTorch (~2.5 GB). Runs in the
        background; training auto-uses the GPU once present and restarted."""
        if gpu["state"] == "installing":
            return jsonify(ok=False, error="already installing"), 409
        gpu["state"] = "installing"; gpu["log"] = ""

        def _do():
            import subprocess
            try:
                p = subprocess.run(
                    [sys.executable, "-m", "pip", "install", "--upgrade",
                     "torch", "torchvision", "--index-url",
                     "https://download.pytorch.org/whl/cu121"],
                    capture_output=True, text=True)
                gpu["log"] = (p.stdout + p.stderr)[-2000:]
                gpu["state"] = "done" if p.returncode == 0 else "error"
            except Exception as e:
                gpu["state"] = "error"; gpu["log"] = str(e)

        import threading
        threading.Thread(target=_do, daemon=True).start()
        return jsonify(ok=True, state="installing")

    @app.get("/api/gpu/install/status")
    def gpu_install_status():
        return jsonify(state=gpu["state"], log=gpu["log"][-1500:])

    # -- camera -------------------------------------------------------------
    @app.get("/api/camera/status")
    def camera_status():
        return jsonify(cam.status())

    @app.get("/api/cameras")
    def cameras_list():
        return jsonify(cameras=discover_cameras(cfg.get("device")),
                       active={"kind": cfg.get("camera_kind"),
                               "source": cam.source})

    @app.post("/api/camera/select")
    def camera_select():
        """Switch to a different camera. body: {kind: oak|usb|ip, source}."""
        nonlocal cam
        body = request.get_json(force=True, silent=True) or {}
        kind = (body.get("kind") or "oak").lower()
        source = body.get("source")
        if kind == "usb" and str(source).lstrip("-").isdigit():
            source = int(source)
        cam.stop()
        updates = {"camera_kind": kind, "camera_source": source}
        if kind == "oak" and source not in (None, "auto"):
            updates["device"] = source        # remember the OAK IP too
        cfg.update(**updates)
        cam = build_camera()
        cam.start()
        return jsonify(cam.status())

    @app.post("/api/camera/start")
    def camera_start():
        """Reconnect the current camera (optionally pointing the OAK at a new IP)."""
        nonlocal cam
        body = request.get_json(force=True, silent=True) or {}
        if body.get("device"):
            cfg.update(device=body["device"], camera_kind="oak", camera_source=None)
            cam.stop()
            cam = build_camera()
        cam.start()
        return jsonify(cam.status())

    @app.post("/api/camera/stop")
    def camera_stop():
        cam.stop()
        return jsonify(cam.status())

    @app.get("/api/camera/stream")
    def camera_stream():
        return Response(cam.mjpeg(),
                        mimetype="multipart/x-mixed-replace; boundary=frame")

    @app.get("/api/camera/snapshot")
    def camera_snapshot():
        jpeg = cam.snapshot()
        if jpeg is None:
            return ("camera not streaming", 503)
        return Response(jpeg, mimetype="image/jpeg")

    # -- dataset ------------------------------------------------------------
    @app.get("/api/dataset/stats")
    def dataset_stats():
        return jsonify(ds.stats())

    @app.get("/api/dataset/health")
    def dataset_health():
        return jsonify(ds.health())

    @app.get("/api/classes")
    def get_classes():
        return jsonify(ds.classes())

    @app.post("/api/classes")
    def add_class():
        body = request.get_json(force=True, silent=True) or {}
        return jsonify(ds.add_class(body.get("name", "")))

    @app.delete("/api/classes/<name>")
    def remove_class(name):
        ds.remove_class(name)
        return jsonify(classes=ds.classes(), stats=ds.stats())

    @app.get("/api/samples")
    def list_samples():
        return jsonify(ds.list_samples())

    @app.post("/api/samples")
    def save_sample():
        """Create a sample, or edit one (pass its `stem`). Boxes may include
        front/back `keypoints`. When editing without a new image, the existing
        image is kept."""
        body = request.get_json(force=True, silent=True) or {}
        img_b64 = body.get("image_b64")
        boxes = body.get("boxes", [])
        stem_in = body.get("stem")
        if img_b64:
            jpeg = base64.b64decode(img_b64)
        elif stem_in:
            jpeg = None                       # editing -> keep existing image
        else:
            jpeg = cam.snapshot()
            if jpeg is None:
                return jsonify(error="no image and camera not streaming"), 400
        try:
            stem = ds.save_sample(jpeg, boxes, stem=stem_in)
        except ValueError as e:
            return jsonify(error=str(e)), 400
        return jsonify(stem=stem, stats=ds.stats())

    @app.get("/api/samples/<stem>/image")
    def sample_image(stem):
        try:
            return Response(ds.image_bytes(stem), mimetype="image/jpeg")
        except FileNotFoundError:
            return ("not found", 404)

    @app.delete("/api/samples/<stem>")
    def delete_sample(stem):
        ds.delete_sample(stem)
        return jsonify(ok=True, stats=ds.stats())

    # -- negatives (teach "not this part") ----------------------------------
    @app.post("/api/negatives")
    def add_negative():
        body = request.get_json(force=True, silent=True) or {}
        cls = body.get("className") or cfg.get("target_class")
        img_b64 = body.get("image_b64")
        if img_b64:
            jpeg = base64.b64decode(img_b64)
        else:
            jpeg = cam.snapshot()
            if jpeg is None:
                return jsonify(error="no image and camera not streaming"), 400
        stem = ds.save_negative(jpeg, cls)
        return jsonify(stem=stem, className=cls, stats=ds.stats())

    @app.get("/api/negatives")
    def list_negatives():
        return jsonify(ds.list_negatives())

    @app.get("/api/negatives/<cls>/<stem>/image")
    def negative_image(cls, stem):
        try:
            return Response(ds.negative_bytes(cls, stem), mimetype="image/jpeg")
        except FileNotFoundError:
            return ("not found", 404)

    @app.delete("/api/negatives/<cls>/<stem>")
    def delete_negative(cls, stem):
        ds.delete_negative(cls, stem)
        return jsonify(ok=True, stats=ds.stats())

    # -- import existing work -----------------------------------------------
    @app.post("/api/import/dataset")
    def import_dataset():
        body = request.get_json(force=True, silent=True) or {}
        path = (body.get("path") or "").strip()
        if not path:
            return jsonify(error="enter the folder path of your dataset"), 400
        try:
            return jsonify(ds.import_dir(path))
        except FileNotFoundError as e:
            return jsonify(error=str(e)), 400

    @app.post("/api/export/dataset")
    def export_dataset():
        body = request.get_json(force=True, silent=True) or {}
        path = (body.get("path") or "").strip()
        if not path:
            return jsonify(error="choose a destination folder"), 400
        try:
            return jsonify(ds.export_zip(path))
        except OSError as e:
            return jsonify(error=str(e)), 400

    @app.post("/api/export/model")
    def export_model():
        """Export a model as a self-contained, runnable bundle (.zip): PyTorch
        .pt + ONNX + classes + the orientation reference + the actual inference
        code (infer.py, orientation.py, orient_template.py) + example.py +
        requirements.txt + README.md. body: {path, model?} (model defaults to
        the active one)."""
        body = request.get_json(force=True, silent=True) or {}
        dest = (body.get("path") or "").strip()
        if not dest:
            return jsonify(error="choose a destination folder"), 400
        dest = Path(dest).expanduser()
        dest.mkdir(parents=True, exist_ok=True)
        name = body.get("model")
        weights = (models_dir / name) if name and (models_dir / name).exists() else active_weights()
        if not weights.exists():
            return jsonify(error="no trained model to export"), 400

        from ultralytics import YOLO
        ymodel = YOLO(str(weights))
        names = ymodel.names
        names = list(names.values()) if isinstance(names, dict) else list(names)

        stage = Path(tempfile.mkdtemp())
        shutil.copy2(weights, stage / "model.pt")
        (stage / "classes.txt").write_text("\n".join(names) + "\n")
        onnx_ok = False
        try:
            onnx_path = Path(ymodel.export(format="onnx", imgsz=640, verbose=False))
            shutil.copy2(onnx_path, stage / "model.onnx")
            onnx_path.unlink(missing_ok=True)
            onnx_ok = True
        except Exception as e:
            (stage / "ONNX_EXPORT_FAILED.txt").write_text(str(e))

        odir = weights.with_name(weights.stem + ".orient")
        legacy = weights.with_name(weights.stem + ".orient.npz")
        has_orient = odir.is_dir() or legacy.exists()
        if odir.is_dir():
            shutil.copytree(odir, stage / "orient")       # per-class references
        elif legacy.exists():
            shutil.copy2(legacy, stage / "orientation.npz")
        (stage / "orientation.json").write_text(json.dumps({
            "has_orientation": has_orient,
            "method": "shape-axis + appearance-template + ORB (see README.md / infer.py)",
            "zero_deg": cfg.get("zero_deg"),
            "template_file": "orientation.npz" if has_orient else None,
        }, indent=2))

        # the actual custom code, so the bundle runs as-is
        assets = HERE / "export_assets"
        for f in ("infer.py", "example.py", "requirements.txt"):
            shutil.copy2(assets / f, stage / f)
        shutil.copy2(HERE / "orientation.py", stage / "orientation.py")
        shutil.copy2(HERE / "orient_template.py", stage / "orient_template.py")
        (stage / "README.md").write_text(_readme_md(names, onnx_ok, has_orient))

        out = dest / "vision-studio-model.zip"
        n = 2
        while out.exists():
            out = dest / f"vision-studio-model-{n}.zip"; n += 1
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            for f in stage.iterdir():
                z.write(f, f.name)
        shutil.rmtree(stage, ignore_errors=True)
        return jsonify(ok=True, path=str(out), onnx=onnx_ok,
                       orientation=has_orient, classes=names)

    @app.post("/api/import/model")
    def import_model():
        body = request.get_json(force=True, silent=True) or {}
        path = Path((body.get("path") or "").strip()).expanduser()
        if not path.exists() or path.suffix != ".pt":
            return jsonify(error="point to a trained .pt model file"), 400
        dest = models_dir / path.name
        shutil.copy2(path, dest)
        tp = path.with_name(path.stem + ".orient.npz")   # legacy orientation sidecar
        if tp.exists():
            shutil.copy2(tp, models_dir / tp.name)
        od = path.with_name(path.stem + ".orient")        # per-class orientation refs
        if od.is_dir():
            shutil.copytree(od, models_dir / od.name, dirs_exist_ok=True)
        # the model knows its own class name(s) — adopt the first as the target
        cls = body.get("class_name")
        if not cls:
            try:
                from ultralytics import YOLO
                names = YOLO(str(dest)).names
                cls = names[0] if isinstance(names, (list, tuple)) else names.get(0)
            except Exception:
                cls = dest.stem
        cfg.update(active_model=dest.name, target_class=cls)
        state["vision"] = Vision(active_weights(), cfg.get("target_class"))
        return jsonify(ok=True, model=dest.name, target_class=cls,
                       models=[p.name for p in sorted(models_dir.glob("*.pt"))])

    # -- training -----------------------------------------------------------
    @app.post("/api/train")
    def train_start():
        body = request.get_json(force=True, silent=True) or {}
        cls = body.get("class_name") or cfg.get("target_class")
        epochs = int(body.get("epochs", 150))
        ds_neg = bool(body.get("dataset_negatives", True))
        multi = bool(body.get("multiclass"))
        size = body.get("model_size", "n")
        try:
            trainer.start(cls, epochs=epochs, dataset_negatives=ds_neg,
                          multiclass=multi, model_size=size)
        except RuntimeError as e:
            return jsonify(error=str(e)), 409
        except ValueError as e:
            return jsonify(error=str(e)), 400
        return jsonify(trainer.status())

    @app.get("/api/train/status")
    def train_status():
        st = trainer.status()
        # when a fresh model finished, make it active
        if st["state"] == "done" and st["weights"]:
            name = Path(st["weights"]).name
            if cfg.get("active_model") != name:
                cfg.update(active_model=name)
                state["vision"] = Vision(active_weights(), cfg.get("target_class"))
        return jsonify(st)

    @app.get("/api/train/preview")
    def train_preview():
        """Held-out validation images WITH predicted boxes, from the last run."""
        imgs = []
        for p in trainer.previews:
            try:
                imgs.append(base64.b64encode(Path(p).read_bytes()).decode())
            except Exception:
                pass
        return jsonify(images=imgs)

    @app.get("/api/models")
    def list_models():
        return jsonify(models=[p.name for p in sorted(models_dir.glob("*.pt"))],
                       active=active_weights().name)

    @app.delete("/api/models/<name>")
    def delete_model(name):
        f = models_dir / name
        if f.suffix != ".pt" or not f.exists():
            return jsonify(ok=False, error="model not found"), 200
        f.unlink()
        f.with_name(f.stem + ".orient.npz").unlink(missing_ok=True)   # legacy sidecar
        odir = f.with_name(f.stem + ".orient")                        # per-class refs
        if odir.is_dir():
            shutil.rmtree(odir)
        if cfg.get("active_model") == name:                            # was active -> reset
            cfg.update(active_model=None)
            state["vision"] = Vision(active_weights(), cfg.get("target_class"))
        return jsonify(ok=True,
                       models=[p.name for p in sorted(models_dir.glob("*.pt"))],
                       active=active_weights().name)

    @app.post("/api/dataset/clear")
    def dataset_clear():
        body = request.get_json(force=True, silent=True) or {}
        cls = body.get("class_name")
        if cls and cls != "all":
            return jsonify(ds.delete_class(cls))
        return jsonify(ds.clear())

    @app.post("/api/models/select")
    def select_model():
        body = request.get_json(force=True, silent=True) or {}
        name = body.get("name")
        if not name or not (models_dir / name).exists():
            return jsonify(ok=False, error="model not found"), 200
        cls = cfg.get("target_class")
        try:  # adopt the model's own class name
            from ultralytics import YOLO
            names = YOLO(str(models_dir / name)).names
            cls = names[0] if isinstance(names, (list, tuple)) else names.get(0)
        except Exception:
            pass
        cfg.update(active_model=name, target_class=cls)
        state["vision"] = Vision(active_weights(), cfg.get("target_class"))
        return jsonify(ok=True, model=name, target_class=cls)

    # -- detection ----------------------------------------------------------
    def _image_from_request(body):
        if body.get("source") == "image" and body.get("data"):
            buf = np.frombuffer(base64.b64decode(body["data"]), np.uint8)
            return cv2.imdecode(buf, cv2.IMREAD_COLOR), None
        jpeg = cam.snapshot()
        if jpeg is None:
            return None, "camera not streaming"
        buf = np.frombuffer(jpeg, np.uint8)
        return cv2.imdecode(buf, cv2.IMREAD_COLOR), None

    @app.post("/api/detect")
    def detect():
        if not state["vision"].ready:
            return jsonify(found=False, error="no model yet — train one, or import a .pt on the Home screen"), 200
        body = request.get_json(force=True, silent=True) or {}
        img, err = _image_from_request(body)
        if img is None:
            return jsonify(found=False, error=err), 200
        conf = float(body.get("conf", 0.25))
        iou = float(body.get("iou", 0.45))
        res = state["vision"].analyze(img, zero_deg=float(cfg.get("zero_deg")), conf=conf, iou=iou)
        if res.get("found"):
            last["img"], last["result"] = img, res
        return jsonify(res)

    @app.post("/api/correct")
    def correct():
        """Teach orientation from a tested frame. body: {flip: bool} for a quick
        180-flip, or {angle_deg: <directed image-space angle>} for a precise
        drawn correction. Saves the last-detected frame as a NEW training sample
        with front/back keypoints placed at the part's ends along the corrected
        direction. Retrain to apply."""
        body = request.get_json(force=True, silent=True) or {}
        flip = bool(body.get("flip"))
        r, img = last["result"], last["img"]
        if r is None or img is None:
            return jsonify(error="run a detection first"), 400
        if body.get("angle_deg") is not None:          # precise drawn angle
            a = math.radians(float(body["angle_deg"]))
        elif r.get("angle_raw") is not None:           # reinforce / 180-flip
            a = math.radians(r["angle_raw"] + (180.0 if flip else 0.0))
        else:
            return jsonify(error="no orientation to correct — draw the arrow instead"), 400
        H, W = img.shape[:2]
        x1, y1, x2, y2 = r["bbox"]
        cx, cy = r["center"]["x"], r["center"]["y"]
        L = 0.4 * max(x2 - x1, y2 - y1)             # well-separated, at the ends
        clamp = lambda v: max(0.0, min(1.0, v))
        box = {
            "className": cfg.get("target_class"),
            "cx": clamp((x1 + x2) / 2 / W), "cy": clamp((y1 + y2) / 2 / H),
            "w": clamp((x2 - x1) / W), "h": clamp((y2 - y1) / H),
            "keypoints": [
                [clamp((cx + L * math.cos(a)) / W), clamp((cy + L * math.sin(a)) / H)],
                [clamp((cx - L * math.cos(a)) / W), clamp((cy - L * math.sin(a)) / H)]],
        }
        ok, enc = cv2.imencode(".jpg", img)
        stem = ds.save_sample(enc.tobytes(), [box])
        return jsonify(ok=True, stem=stem, flipped=flip, stats=ds.stats())

    @app.post("/api/calibrate")
    def calibrate():
        if not state["vision"].ready:
            return jsonify(ok=False, error="no trained model yet"), 200
        body = request.get_json(force=True, silent=True) or {}
        img, err = _image_from_request(body)
        if img is None:
            return jsonify(ok=False, error=err), 200
        r = state["vision"].analyze(img, annotate=False)
        if not r["found"] or r["angle_raw"] is None:
            return jsonify(ok=False, error="could not measure the part's angle"), 200
        cfg.update(zero_deg=float(r["angle_raw"]))
        return jsonify(ok=True, zero=float(r["angle_raw"]))

    # lambda sees the latest `cam` (reassigned on camera switch) at call time
    app.stop_camera = lambda: cam.stop()
    return app


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8600)
    p.add_argument("--data-dir", default=str(HERE.parent / "data"))
    p.add_argument("--device", default=None, help="OAK IP (overrides config)")
    p.add_argument("--no-camera", action="store_true")
    args = p.parse_args()
    app = create_app(args.data_dir, device=args.device, start_camera=not args.no_camera)

    # Release the camera cleanly on exit so the OAK isn't left in a stuck
    # session (Electron stops the backend with SIGTERM).
    import atexit
    import signal
    atexit.register(lambda: app.stop_camera())

    def _shutdown(*_):
        app.stop_camera()
        sys.exit(0)
    signal.signal(signal.SIGTERM, _shutdown)

    print(f"Vision Studio backend on http://127.0.0.1:{args.port}", flush=True)
    app.run(host="127.0.0.1", port=args.port, threaded=True)


if __name__ == "__main__":
    main()
