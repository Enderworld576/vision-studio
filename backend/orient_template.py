"""Direction / facing from features *inside* the part (part-agnostic).

YOLO finds and boxes the part well but can't tell which way it faces. This
module reads the internal appearance to recover the facing angle, using a
learned canonical reference built from the user's labels. Two complementary
methods (an ensemble — "multiple kinds of image detection"):

  1. Rotational appearance correlation: rotate the ROI to many angles, CLAHE-
     enhance, and keep the angle whose appearance best matches the canonical
     mean. Works on low-contrast/near-symmetric parts and even square parts
     (where the silhouette axis is ambiguous), because it uses every pixel.
  2. ORB feature matching: match internal keypoint features of the ROI against
     the canonical reference and estimate the rotation directly. Strong for
     detailed/textured parts; contributes when it finds confident matches.

The shape axis (orientation.shape_axis) narrows the correlation search to two
candidates for elongated parts; for round parts we search the full circle.
"""

import math
import re

import cv2
import numpy as np

SZ = 80


def safe_name(s):
    """Filesystem-safe class name (for per-class orientation reference files)."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(s)).strip("_") or "class"


def _gray_clahe(bgr):
    g = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    return cv2.createCLAHE(2.0, (8, 8)).apply(g)


def _aligned(img, box_px, ref_deg, sz=SZ):
    """Square CLAHE patch of the part, rotated so ref_deg points to +x."""
    x1, y1, x2, y2 = box_px
    cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
    side = max(x2 - x1, y2 - y1) * 1.25
    if side < 2:
        return None
    s = sz / side
    M = cv2.getRotationMatrix2D((cx, cy), ref_deg, s)
    M[0, 2] += sz / 2 - cx
    M[1, 2] += sz / 2 - cy
    p = cv2.warpAffine(img, M, (sz, sz), borderMode=cv2.BORDER_REPLICATE)
    return _gray_clahe(p)


def _norm(patch):
    g = patch.astype(np.float32)
    g -= g.mean()
    n = np.linalg.norm(g)
    return g / n if n > 1e-6 else None


def _sharpness(g):
    return float(cv2.Laplacian(g, cv2.CV_64F).var())


# ---- training: build the canonical reference -----------------------------
def build_reference(items, sz=SZ):
    """items: iterable of (bgr_img, box_px, front_deg). Returns a dict with the
    normalized mean template and a sharp reference image (both canonical, front
    pointing +x), or None."""
    acc = np.zeros((sz, sz), np.float32)
    k = 0
    best_ref, best_sharp = None, -1.0
    for img, box, fd in items:
        a = _aligned(img, box, fd, sz)
        if a is None:
            continue
        nm = _norm(a)
        if nm is None:
            continue
        acc += nm
        k += 1
        sh = _sharpness(a)
        if sh > best_sharp:
            best_sharp, best_ref = sh, a
    if k == 0:
        return None
    mean = _norm(acc / k)
    return {"template": mean, "ref": best_ref, "sz": sz} if mean is not None else None


def save(path, ref):
    np.savez(str(path), template=ref["template"], ref=ref["ref"], sz=ref["sz"])


def load(path):
    d = np.load(str(path))
    return {"template": d["template"], "ref": d["ref"], "sz": int(d["sz"])}


# ---- inference: resolve facing angle -------------------------------------
def _corr_at(img, box_px, deg, template, sz):
    nm = _norm(_aligned(img, box_px, deg, sz)) if True else None
    return float((nm * template).sum()) if nm is not None else -1.0


def _refine(img, box_px, center_deg, template, sz, span=12, step=4):
    best_a, best_c = center_deg, -1.0
    for d in range(-span, span + 1, step):
        a = center_deg + d
        c = _corr_at(img, box_px, a, template, sz)
        if c > best_c:
            best_c, best_a = c, a
    return best_a % 360.0, best_c


def _orb_angle(img, box_px, ref_img, sz):
    """Estimate facing via ORB feature matching to the canonical reference.
    Returns (angle_deg, inliers) or None. ref is canonical (front -> +x)."""
    roi = _aligned(img, box_px, 0.0, sz)   # upright crop (no rotation applied)
    if roi is None:
        return None
    orb = cv2.ORB_create(500)
    k1, d1 = orb.detectAndCompute(roi, None)
    k2, d2 = orb.detectAndCompute(ref_img, None)
    if d1 is None or d2 is None or len(k1) < 10 or len(k2) < 10:
        return None
    bf = cv2.BFMatcher(cv2.NORM_HAMMING)
    good = [m for m, n in bf.knnMatch(d1, d2, k=2) if m.distance < 0.75 * n.distance]
    if len(good) < 10:
        return None
    src = np.float32([k1[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
    dst = np.float32([k2[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)
    M, inl = cv2.estimateAffinePartial2D(src, dst, method=cv2.RANSAC, ransacReprojThreshold=4)
    if M is None or inl is None or int(inl.sum()) < 8:
        return None
    # M rotates ROI -> canonical(front=+x); the part's front in the image is the
    # inverse rotation of M applied to +x.
    rot = math.atan2(M[1, 0], M[0, 0])     # ROI->canonical rotation
    angle = math.degrees(-rot) % 360.0
    return angle, int(inl.sum())


def orient(img, box_px, axis_deg, ref):
    """Return (directed_angle_deg, method, confidence). axis_deg is the
    undirected shape axis (0..180) or None for round parts."""
    template, ref_img, sz = ref["template"], ref["ref"], ref["sz"]
    if axis_deg is not None:
        # Decide front/back at the EXACT axis (robust), then refine only the
        # winner for angle precision. (Refining both candidates first lets the
        # wrong flip's local search find a spurious higher correlation.)
        cf = _corr_at(img, box_px, axis_deg, template, sz)
        cb = _corr_at(img, box_px, axis_deg + 180.0, template, sz)
        base = axis_deg if cf >= cb else axis_deg + 180.0
        corr_angle, corr_conf = _refine(img, box_px, base, template, sz)
    else:
        # round part: search the full circle
        corr_angle, corr_conf = 0.0, -1.0
        for cd in range(0, 360, 20):
            a, c = _refine(img, box_px, cd, template, sz)
            if c > corr_conf:
                corr_angle, corr_conf = a, c

    orb = _orb_angle(img, box_px, ref_img, sz)
    if orb is not None:
        orb_angle, inl = orb
        # trust ORB when it has many inliers AND broadly agrees with the
        # correlation winner (guards against a spurious 180 from few matches)
        if inl >= 20 and abs((orb_angle - corr_angle + 180) % 360 - 180) < 60:
            return orb_angle, "features+template", float(inl)
    return corr_angle, "template", float(corr_conf)
