#!/usr/bin/env python3
"""Shape-based axis estimation (part-agnostic).

Stage 2a of orientation: given a detected box, segment the part from the
background (GrabCut, seeded by the box) and take the long axis of its
minimum-area rectangle. This gives the orientation *line* (0..180, undirected)
plus the refined center and the rotated box, for ANY part — no part-specific
colors or features. Direction (which way it faces) is resolved separately by
the appearance/feature methods in orient_template.py.

Image y points down, so angles increase clockwise.
"""

import math

import cv2
import numpy as np


def _largest_central_contour(mask, roi_wh):
    """Largest contour whose centroid sits near the ROI center (the detection
    box is centered on the part, so border blobs are background)."""
    cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cnts:
        return None
    w, h = roi_wh
    cx0, cy0 = w / 2, h / 2
    best, best_score = None, -1.0
    for c in cnts:
        area = cv2.contourArea(c)
        if area < 0.01 * w * h:
            continue
        M = cv2.moments(c)
        if M["m00"] == 0:
            continue
        cx, cy = M["m10"] / M["m00"], M["m01"] / M["m00"]
        dist = math.hypot(cx - cx0, cy - cy0) / math.hypot(cx0, cy0)
        score = area * (1.0 - 0.5 * dist)
        if score > best_score:
            best, best_score = c, score
    return best


def _grabcut_silhouette(roi):
    """Foreground mask of the part via GrabCut seeded by the (padded) box."""
    h, w = roi.shape[:2]
    m = int(0.10 * min(h, w)) or 1
    rect = (m, m, max(1, w - 2 * m), max(1, h - 2 * m))
    gc = np.zeros((h, w), np.uint8)
    bgd, fgd = np.zeros((1, 65), np.float64), np.zeros((1, 65), np.float64)
    try:
        cv2.grabCut(roi, gc, rect, bgd, fgd, 4, cv2.GC_INIT_WITH_RECT)
    except cv2.error:
        return None
    fg = np.where((gc == cv2.GC_FGD) | (gc == cv2.GC_PR_FGD), 255, 0).astype(np.uint8)
    k = np.ones((5, 5), np.uint8)
    fg = cv2.morphologyEx(fg, cv2.MORPH_CLOSE, k, iterations=2)
    fg = cv2.morphologyEx(fg, cv2.MORPH_OPEN, k)
    return fg


def long_axis_angle(box_pts):
    """Angle (deg, 0..180) of the longest edge of a 4-point rotated rect."""
    edges = [box_pts[(i + 1) % 4] - box_pts[i] for i in range(4)]
    e = max(edges, key=lambda v: float(np.hypot(v[0], v[1])))
    return math.degrees(math.atan2(e[1], e[0])) % 180.0


def shape_axis(image_bgr, bbox, pad=0.15):
    """Undirected axis of the part inside `bbox`=(x1,y1,x2,y2) full-image px.

    Returns: center (px), angle_deg (0..180) or None, box (4x2 px) or None,
    elongation (long/short side ratio), ok (False if segmentation failed)."""
    H, W = image_bgr.shape[:2]
    x1, y1, x2, y2 = bbox
    bw, bh = x2 - x1, y2 - y1
    px, py = int(bw * pad), int(bh * pad)
    rx1, ry1 = max(0, int(x1 - px)), max(0, int(y1 - py))
    rx2, ry2 = min(W, int(x2 + px)), min(H, int(y2 + py))
    roi = image_bgr[ry1:ry2, rx1:rx2]
    fallback = {"center": ((x1 + x2) / 2.0, (y1 + y2) / 2.0),
                "angle_deg": None, "box": None, "elongation": 1.0, "ok": False}
    if roi.size == 0:
        return fallback
    sil = _grabcut_silhouette(roi)
    if sil is None:
        return fallback
    cnt = _largest_central_contour(sil, (rx2 - rx1, ry2 - ry1))
    if cnt is None:
        return fallback
    (rcx, rcy), (rw, rh), _ = cv2.minAreaRect(cnt)
    box = cv2.boxPoints(((rcx, rcy), (rw, rh), _))
    angle = long_axis_angle(box)
    M = cv2.moments(cnt)
    cx = M["m10"] / M["m00"] + rx1
    cy = M["m01"] / M["m00"] + ry1
    box[:, 0] += rx1
    box[:, 1] += ry1
    elong = (max(rw, rh) / min(rw, rh)) if min(rw, rh) > 1 else 1.0
    return {"center": (cx, cy), "angle_deg": angle, "box": box,
            "elongation": float(elong), "ok": True}


# Back-compat alias (older callers used estimate_pose for the axis).
def estimate_pose(image_bgr, bbox, pad=0.15):
    r = shape_axis(image_bgr, bbox, pad)
    r["directed"] = False
    r["keypoints"] = None
    return r


def relative_angle(angle_deg, zero_deg, symmetric_180=False):
    """Angle relative to the calibrated default, wrapped to a tidy range."""
    d = angle_deg - zero_deg
    period = 180.0 if symmetric_180 else 360.0
    d = (d + period / 2) % period - period / 2
    return d
