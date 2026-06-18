"""Detection + orientation engine (part-agnostic, multi-class).

Stage 1: the trained YOLO detector finds EVERY instance of EVERY class it knows
in the frame (near-duplicate boxes of the same object/class are removed).
Stage 2: for each instance, orientation is read from the part itself — a stable
axis from its shape (orientation.shape_axis) and the facing from internal
appearance + ORB features vs a learned reference (orient_template.orient). A
model may carry one orientation reference per class (optional add-on).
"""

import base64
import math
from pathlib import Path

import cv2
import numpy as np
from ultralytics import YOLO

import orientation as ori
import orient_template as otmpl

# Distinct box colors per class id (BGR).
_PALETTE = [(76, 141, 255), (63, 191, 127), (255, 150, 76), (180, 120, 255),
            (90, 200, 200), (255, 90, 150), (120, 200, 80), (200, 170, 60)]


def _box_overlap(a, b):
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    if inter <= 0:
        return 0.0, 0.0
    aa = max(1.0, (a[2] - a[0]) * (a[3] - a[1]))
    ba = max(1.0, (b[2] - b[0]) * (b[3] - b[1]))
    return inter / (aa + ba - inter), inter / min(aa, ba)


class Vision:
    def __init__(self, weights, class_name="part"):
        self.weights = str(weights)
        self.class_name = class_name
        self._model = None
        # Orientation references keyed by class name; "*" is a wildcard used by
        # legacy single-class models. Loaded from <model>.orient/<class>.npz, or
        # the legacy <model>.orient.npz single file.
        self._refs = {}
        w = Path(self.weights)
        odir = w.with_name(w.stem + ".orient")
        if odir.is_dir():
            for f in odir.glob("*.npz"):
                try:
                    self._refs[f.stem] = otmpl.load(f)
                except Exception:
                    pass
        legacy = w.with_name(w.stem + ".orient.npz")
        if legacy.exists() and not self._refs:
            try:
                self._refs["*"] = otmpl.load(legacy)
            except Exception:
                pass

    @property
    def ready(self):
        return Path(self.weights).exists()

    @property
    def has_orientation(self):
        if self._refs:
            return True
        try:                                   # a pose model carries orientation itself
            return getattr(self._load(), "task", "") == "pose"
        except Exception:
            return False

    def _load(self):
        if self._model is None:
            if not self.ready:
                raise FileNotFoundError(f"model not found: {self.weights}")
            self._model = YOLO(self.weights)
        return self._model

    def _ref_for(self, class_name):
        return self._refs.get(otmpl.safe_name(class_name)) or self._refs.get("*")

    def _pose_orient(self, bbox, kxy, kconf):
        """Orientation straight from the model's front/back keypoints.
        kxy: (2,2) [front,back] px; kconf: (2,) or None. Returns the same dict
        shape as _orient. Low keypoint confidence (e.g. a box-only class) ->
        no direction."""
        x1, y1, x2, y2 = bbox
        center = ((x1 + x2) / 2.0, (y1 + y2) / 2.0)
        front, back = kxy[0], kxy[1]
        vis = kconf is None or float(min(kconf)) >= 0.30
        d = math.hypot(front[0] - back[0], front[1] - back[1])
        if vis and d > 1.0:
            ang = math.degrees(math.atan2(front[1] - back[1], front[0] - back[0])) % 360.0
            return {"center": center, "box": None, "angle_deg": ang, "directed": True, "method": "pose"}
        return {"center": center, "box": None, "angle_deg": None, "directed": False, "method": "none"}

    def _orient(self, img, bbox, class_name):
        ax = ori.shape_axis(img, bbox)
        center, box = ax["center"], ax["box"]
        ref = self._ref_for(class_name)
        if ref is not None:
            a = ax["angle_deg"] if ax["ok"] else None
            ang, method, _ = otmpl.orient(img, bbox, a, ref)
            return {"center": center, "box": box, "angle_deg": ang,
                    "directed": True, "method": method}
        if ax["ok"] and ax["angle_deg"] is not None:
            return {"center": center, "box": box, "angle_deg": ax["angle_deg"],
                    "directed": False, "method": "axis"}
        return {"center": center, "box": None, "angle_deg": None,
                "directed": False, "method": "none"}

    def _annotate(self, img, bbox, pose, rel, conf, cid, cname):
        color = _PALETTE[cid % len(_PALETTE)]
        x1, y1, x2, y2 = [int(v) for v in bbox]
        cv2.rectangle(img, (x1, y1), (x2, y2), color, 2)
        cx, cy = pose["center"]
        if pose.get("angle_deg") is not None:
            a = math.radians(pose["angle_deg"])
            L = 0.6 * max(x2 - x1, y2 - y1)
            cv2.arrowedLine(img, (int(cx), int(cy)),
                            (int(cx + L * math.cos(a)), int(cy + L * math.sin(a))),
                            (255, 0, 0), 3, tipLength=0.3)
        cv2.circle(img, (int(cx), int(cy)), 4, (0, 0, 255), -1)
        deg = "" if rel is None else f"  {rel:+.0f}deg"
        cv2.putText(img, f"{cname} {conf*100:.0f}%{deg}", (x1, max(22, y1 - 8)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2, cv2.LINE_AA)

    def analyze(self, img, zero_deg=0.0, conf=0.25, iou=0.45, annotate=True, max_det=100):
        model = self._load()
        names = model.names                       # {id: name}
        res = model.predict(img, conf=conf, iou=iou, verbose=False, max_det=max_det)[0]
        if res.boxes is None or len(res.boxes) == 0:
            return {"found": False, "count": 0, "detections": [], "classes": [], "zero": zero_deg}
        confs = res.boxes.conf.cpu().numpy()
        xyxy = res.boxes.xyxy.cpu().numpy()
        cls = res.boxes.cls.cpu().numpy().astype(int)
        order = np.argsort(-confs)
        # Learned orientation: a pose model returns front/back keypoints per box.
        is_pose = getattr(model, "task", "") == "pose" and res.keypoints is not None
        kp_xy = res.keypoints.xy.cpu().numpy() if is_pose else None
        kp_cf = (res.keypoints.conf.cpu().numpy()
                 if is_pose and res.keypoints.conf is not None else None)

        # de-duplicate per class (different classes may legitimately overlap)
        keep = []
        for idx in order:
            bb = xyxy[idx]
            dup = False
            for k in keep:
                if cls[idx] != cls[k]:
                    continue
                iou, contain = _box_overlap(bb, xyxy[k])
                if iou > 0.6 or contain > 0.8:
                    dup = True
                    break
            if not dup:
                keep.append(idx)

        out = img.copy() if annotate else None
        dets = []
        for idx in keep:
            bbox = xyxy[idx]
            c = float(confs[idx])
            cid = int(cls[idx])
            cname = names[cid] if isinstance(names, (list, tuple)) else names.get(cid, str(cid))
            if is_pose:
                pose = self._pose_orient(bbox, kp_xy[idx], kp_cf[idx] if kp_cf is not None else None)
            else:
                pose = self._orient(img, bbox, cname)
            rel = (ori.relative_angle(pose["angle_deg"], zero_deg)
                   if pose["angle_deg"] is not None else None)
            if annotate:
                self._annotate(out, bbox, pose, rel, c, cid, cname)
            dets.append({
                "class": cname, "class_id": cid, "conf": c,
                "center": {"x": int(pose["center"][0]), "y": int(pose["center"][1])},
                "bbox": [float(v) for v in bbox],
                "angle_raw": pose["angle_deg"],
                "angle_rel": rel if rel is not None else 0.0,
                "directed": pose["directed"], "method": pose["method"],
            })

        present = sorted({d["class"] for d in dets})
        result = {"found": True, "count": len(dets), "detections": dets,
                  "classes": present, "zero": zero_deg}
        result.update({k: dets[0][k] for k in            # mirror primary for the panels
                       ("conf", "center", "bbox", "angle_raw", "angle_rel",
                        "directed", "method", "class")})
        if annotate:
            result["image_b64"] = base64.b64encode(cv2.imencode(".jpg", out)[1]).decode()
            result.update(self._object_views(img, dets[0]["bbox"]))
        return result

    def _object_views(self, img, bbox, pad=0.1):
        """Close-up crop + grayscale + 256-bin intensity histogram of the part."""
        H, W = img.shape[:2]
        x1, y1, x2, y2 = [int(v) for v in bbox]
        p = int(pad * max(x2 - x1, y2 - y1))
        cx1, cy1 = max(0, x1 - p), max(0, y1 - p)
        cx2, cy2 = min(W, x2 + p), min(H, y2 + p)
        crop = img[cy1:cy2, cx1:cx2]
        if crop.size == 0:
            return {}
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        hist = cv2.calcHist([gray], [0], None, [256], [0, 256]).flatten().astype(int).tolist()
        enc = lambda im: base64.b64encode(cv2.imencode(".jpg", im)[1]).decode()
        return {"crop_b64": enc(crop),
                "gray_b64": enc(cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)),
                "hist": hist}
