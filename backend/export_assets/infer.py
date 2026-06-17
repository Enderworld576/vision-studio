"""Vision Studio — standalone inference (drop-in).

Reproduces the full Vision Studio pipeline outside the app:
  1. YOLO detector (model.pt or model.onnx) finds EVERY instance of EVERY class,
     then near-duplicate boxes of the same object/class are removed.
  2. Each instance's orientation is read from the part itself — a stable AXIS
     from its silhouette (orientation.py), and the FACING from internal
     appearance + ORB features vs a learned reference (orient_template.py).
     A model may carry one orientation reference per class.

Depends only on `ultralytics`, `opencv-python`, `numpy`, and the sibling modules
shipped with it (orientation.py, orient_template.py).

    from infer import VisionStudio
    import cv2
    vs = VisionStudio("model.pt")            # finds orient/ or orientation.npz automatically
    for d in vs.detect(cv2.imread("image.jpg")):
        print(d["class"], d["conf"], d["center"], d["angle_deg"], d["method"], d["bbox"])
"""

import math
from pathlib import Path

import cv2
import numpy as np
from ultralytics import YOLO

import orientation as ori
import orient_template as otmpl

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


class VisionStudio:
    def __init__(self, weights, orient="orient", conf=0.25):
        """weights: model.pt or model.onnx. Orientation references are loaded
        from the `orient/` folder (per class) or `orientation.npz` (legacy)."""
        self.model = YOLO(str(weights))
        self.conf = conf
        self.refs = {}
        od = Path(orient)
        if od.is_dir():
            for f in od.glob("*.npz"):
                try:
                    self.refs[f.stem] = otmpl.load(f)
                except Exception:
                    pass
        elif Path("orientation.npz").exists():
            try:
                self.refs["*"] = otmpl.load("orientation.npz")
            except Exception:
                pass

    def _ref_for(self, name):
        return self.refs.get(otmpl.safe_name(name)) or self.refs.get("*")

    def detect(self, img_bgr, max_det=100):
        names = self.model.names
        res = self.model.predict(img_bgr, conf=self.conf, verbose=False, max_det=max_det)[0]
        if res.boxes is None or len(res.boxes) == 0:
            return []
        confs = res.boxes.conf.cpu().numpy()
        xyxy = res.boxes.xyxy.cpu().numpy()
        cls = res.boxes.cls.cpu().numpy().astype(int)
        order = np.argsort(-confs)

        keep = []                                   # de-dup per class
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

        out = []
        for idx in keep:
            bb = xyxy[idx]
            cid = int(cls[idx])
            cname = names[cid] if isinstance(names, (list, tuple)) else names.get(cid, str(cid))
            ax = ori.shape_axis(img_bgr, bb)
            angle, directed, method = None, False, "none"
            ref = self._ref_for(cname)
            if ref is not None:
                a = ax["angle_deg"] if ax["ok"] else None
                angle, method, _ = otmpl.orient(img_bgr, bb, a, ref)
                directed = True
            elif ax["ok"] and ax["angle_deg"] is not None:
                angle, method = ax["angle_deg"], "axis"
            out.append({
                "class": cname, "class_id": cid, "conf": float(confs[idx]),
                "bbox": [float(v) for v in bb],
                "center": [float(ax["center"][0]), float(ax["center"][1])],
                "angle_deg": angle, "directed": directed, "method": method,
            })
        return out

    def draw(self, img_bgr, dets):
        for d in dets:
            color = _PALETTE[d["class_id"] % len(_PALETTE)]
            x1, y1, x2, y2 = [int(v) for v in d["bbox"]]
            cv2.rectangle(img_bgr, (x1, y1), (x2, y2), color, 2)
            cx, cy = [int(v) for v in d["center"]]
            cv2.circle(img_bgr, (cx, cy), 4, (0, 0, 255), -1)
            if d["angle_deg"] is not None:
                a = math.radians(d["angle_deg"])
                L = 0.6 * max(x2 - x1, y2 - y1)
                cv2.arrowedLine(img_bgr, (cx, cy),
                                (int(cx + L * math.cos(a)), int(cy + L * math.sin(a))),
                                (255, 0, 0), 3, tipLength=0.3)
            cv2.putText(img_bgr, f"{d['class']} {d['conf']*100:.0f}%", (x1, max(20, y1 - 6)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2, cv2.LINE_AA)
        return img_bgr
