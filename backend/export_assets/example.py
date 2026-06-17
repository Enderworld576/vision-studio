"""Minimal usage of the exported Vision Studio model.

    python example.py path/to/image.jpg
"""
import sys

import cv2

from infer import VisionStudio

img_path = sys.argv[1] if len(sys.argv) > 1 else "test.jpg"
vs = VisionStudio("model.pt", "orientation.npz")   # orientation.npz is optional

img = cv2.imread(img_path)
if img is None:
    raise SystemExit(f"could not read {img_path}")

dets = vs.detect(img)
print(f"{len(dets)} instance(s) found:")
for i, d in enumerate(dets):
    ang = "n/a" if d["angle_deg"] is None else f"{d['angle_deg']:.1f} deg ({d['method']})"
    print(f"  [{i}] {d['class']}  conf={d['conf']:.2f}  center={d['center']}  angle={ang}  bbox={d['bbox']}")

cv2.imwrite("annotated.jpg", vs.draw(img.copy(), dets))
print("wrote annotated.jpg")
