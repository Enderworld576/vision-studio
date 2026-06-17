"""YOLO dataset on disk (images + labels + classes), as a simple library.

Ported from the data-collector's IPC handlers. One flat dataset of labeled
images; training derives its own train/val split from it. Coordinates are
normalized YOLO boxes (class cx cy w h, all 0..1).
"""

import shutil
import threading
import time
import zipfile
from pathlib import Path

IMG_EXT = (".jpg", ".jpeg", ".png")


class Dataset:
    def __init__(self, root):
        self.root = Path(root)
        self.images = self.root / "images"
        self.labels = self.root / "labels"
        self.negatives = self.root / "negatives"   # per-class "not this part" images
        self.classes_file = self.root / "classes.txt"
        self.ensure()

    def ensure(self):
        self.images.mkdir(parents=True, exist_ok=True)
        self.labels.mkdir(parents=True, exist_ok=True)
        self.negatives.mkdir(parents=True, exist_ok=True)
        if not self.classes_file.exists():
            self.classes_file.write_text("")

    @staticmethod
    def _safe(name):
        return (name or "part").replace("/", "_").replace("\\", "_").strip() or "part"

    # -- negatives (hard negatives / background for a class) -----------------
    def save_negative(self, jpeg_bytes, class_name):
        if not jpeg_bytes:
            raise ValueError("no image")
        d = self.negatives / self._safe(class_name)
        d.mkdir(parents=True, exist_ok=True)
        stem = self._stamp()
        (d / f"{stem}.jpg").write_bytes(jpeg_bytes)
        return stem

    def list_negatives(self):
        out = []
        if self.negatives.exists():
            for d in self.negatives.iterdir():
                if d.is_dir():
                    for img in d.glob("*.jpg"):
                        out.append({"stem": img.stem, "className": d.name})
        out.sort(key=lambda x: x["stem"], reverse=True)
        return out

    def negatives_for(self, class_name):
        d = self.negatives / self._safe(class_name)
        return sorted(d.glob("*.jpg")) if d.exists() else []

    def negative_bytes(self, class_name, stem):
        return (self.negatives / self._safe(class_name) / f"{Path(stem).name}.jpg").read_bytes()

    def delete_negative(self, class_name, stem):
        (self.negatives / self._safe(class_name) / f"{Path(stem).name}.jpg").unlink(missing_ok=True)

    # -- classes ------------------------------------------------------------
    def classes(self):
        return [l.strip() for l in self.classes_file.read_text().splitlines() if l.strip()]

    def _write_classes(self, names):
        self.classes_file.write_text("\n".join(names) + ("\n" if names else ""))
        yaml = (f"path: {self.root}\ntrain: images\nval: images\n"
                f"nc: {len(names)}\nnames: [{', '.join(repr(n) for n in names)}]\n")
        (self.root / "data.yaml").write_text(yaml)

    def class_index(self, name):
        names = self.classes()
        if name not in names:
            names.append(name)
            self._write_classes(names)
        return self.classes().index(name)

    def add_class(self, name):
        name = (name or "").strip()
        if not name:
            raise ValueError("empty class name")
        self.class_index(name)
        return self.classes()

    # -- samples ------------------------------------------------------------
    _seq = 0
    _seq_lock = threading.Lock()

    def _stamp(self):
        # time + ms + a rolling counter, so rapid batch saves never collide
        with Dataset._seq_lock:
            Dataset._seq = (Dataset._seq + 1) % 1000
            n = Dataset._seq
        return (time.strftime("%Y-%m-%dT%H-%M-%S")
                + f"-{int((time.time() % 1) * 1000):03d}{n:03d}")

    def _label_line(self, idx, b):
        """YOLO line: `idx cx cy w h [fx fy 2 bx by 2]`. Keypoints (front, back)
        are appended only when present, so box-only labels stay 5 fields."""
        line = f"{idx} {b['cx']:.6f} {b['cy']:.6f} {b['w']:.6f} {b['h']:.6f}"
        kps = b.get("keypoints")
        if kps and len(kps) == 2:
            for (x, y) in kps:
                line += f" {x:.6f} {y:.6f} 2"
        return line

    def save_sample(self, jpeg_bytes, boxes, stem=None):
        """Save (or, when `stem` is given, overwrite) a labeled sample.

        boxes: [{className, cx, cy, w, h, keypoints?: [[fx,fy],[bx,by]]}]
        (all normalized). When editing an existing sample, pass its stem; the
        image may be omitted (the existing image is kept)."""
        if not boxes:
            raise ValueError("no annotations to save")
        if stem is None:
            stem = self._stamp()
        img_path = self.images / f"{stem}.jpg"
        if jpeg_bytes:
            img_path.write_bytes(jpeg_bytes)
        elif not img_path.exists():
            raise ValueError("no image")
        lines = [self._label_line(self.class_index(b["className"]), b) for b in boxes]
        (self.labels / f"{stem}.txt").write_text("\n".join(lines) + "\n")
        return stem

    def list_samples(self):
        names = self.classes()
        out = []
        for lbl in self.labels.glob("*.txt"):
            stem = lbl.stem
            if not (self.images / f"{stem}.jpg").exists():
                continue
            boxes = []
            for line in lbl.read_text().splitlines():
                p = line.split()
                if len(p) < 5:
                    continue
                cid = int(float(p[0]))
                kps = None
                if len(p) >= 11:   # idx + 4 box + 2*(x,y,v)
                    kps = [[float(p[5]), float(p[6])], [float(p[8]), float(p[9])]]
                boxes.append({
                    "classId": cid,
                    "className": names[cid] if cid < len(names) else f"class {cid}",
                    "cx": float(p[1]), "cy": float(p[2]),
                    "w": float(p[3]), "h": float(p[4]),
                    "keypoints": kps,
                })
            out.append({"stem": stem, "boxes": boxes})
        out.sort(key=lambda s: s["stem"], reverse=True)
        return out

    def image_bytes(self, stem):
        return (self.images / f"{Path(stem).name}.jpg").read_bytes()

    def delete_sample(self, stem):
        stem = Path(stem).name
        (self.images / f"{stem}.jpg").unlink(missing_ok=True)
        (self.labels / f"{stem}.txt").unlink(missing_ok=True)
        return stem

    def import_dir(self, src):
        """Import a YOLO dataset folder (images + labels [+ classes.txt]) into
        this dataset. Handles flat or train/val layouts; merges class names by
        name (remapping label indices); skips images already present."""
        src = Path(src).expanduser()
        if not src.exists():
            raise FileNotFoundError(f"folder not found: {src}")
        labels = {p.stem: p for p in src.rglob("*.txt") if p.name != "classes.txt"}
        src_classes = []
        for cf in src.rglob("classes.txt"):
            src_classes = [l.strip() for l in cf.read_text().splitlines() if l.strip()]
            break
        cache = {}

        def remap(i):
            name = src_classes[i] if i < len(src_classes) else f"class{i}"
            if name not in cache:
                cache[name] = self.class_index(name)
            return cache[name]

        imported, skipped = 0, 0
        for img in src.rglob("*"):
            if img.suffix.lower() not in IMG_EXT:
                continue
            lbl = labels.get(img.stem)
            if not lbl:
                continue
            dst = self.images / f"{img.stem}.jpg"
            if dst.exists():
                skipped += 1
                continue
            shutil.copy2(img, dst)
            out = []
            for line in lbl.read_text().splitlines():
                p = line.split()
                if len(p) < 5:
                    continue
                out.append(f"{remap(int(float(p[0])))} {' '.join(p[1:])}")
            (self.labels / f"{img.stem}.txt").write_text("\n".join(out) + "\n")
            imported += 1
        return {"imported": imported, "skipped": skipped, "stats": self.stats()}

    def export_zip(self, dest_dir):
        """Export the whole dataset as a portable, self-contained YOLO `.zip`
        (images/ + labels/ + classes.txt + data.yaml) into dest_dir. Usable
        directly by other YOLO tools, or re-imported here later."""
        dest = Path(dest_dir).expanduser()
        if not dest.exists():
            dest.mkdir(parents=True, exist_ok=True)
        if not dest.is_dir():
            raise NotADirectoryError(f"not a folder: {dest}")
        out = dest / "vision-studio-dataset.zip"
        n = 2
        while out.exists():
            out = dest / f"vision-studio-dataset-{n}.zip"
            n += 1
        samples = self.list_samples()
        classes = self.classes()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            for s in samples:
                img = self.images / f"{s['stem']}.jpg"
                lbl = self.labels / f"{s['stem']}.txt"
                if img.exists():
                    z.write(img, f"images/{img.name}")
                if lbl.exists():
                    z.write(lbl, f"labels/{lbl.name}")
            z.writestr("classes.txt", "\n".join(classes) + "\n")
            z.writestr("data.yaml",
                       "path: .\ntrain: images\nval: images\n"
                       f"nc: {len(classes)}\n"
                       f"names: [{', '.join(repr(c) for c in classes)}]\n")
        return {"path": str(out), "images": len(samples), "classes": len(classes)}

    def delete_class(self, class_name):
        """Delete every sample (and negatives) of one part. Images that ALSO
        contain other parts are kept, with just this part's boxes removed.
        The class name stays in classes.txt so other labels keep their indices."""
        names = self.classes()
        if class_name not in names:
            return self.stats()
        target = names.index(class_name)
        for lbl in list(self.labels.glob("*.txt")):
            lines = lbl.read_text().splitlines()
            keep = [l for l in lines if l.split() and int(float(l.split()[0])) != target]
            if not keep:
                (self.images / f"{lbl.stem}.jpg").unlink(missing_ok=True)
                lbl.unlink(missing_ok=True)
            elif len(keep) != len(lines):
                lbl.write_text("\n".join(keep) + "\n")
        nd = self.negatives / self._safe(class_name)
        if nd.exists():
            shutil.rmtree(nd)
        return self.stats()

    def remove_class(self, class_name):
        """Fully remove a class: delete its images/labels/negatives, drop it from
        the class list, and reindex the remaining classes' labels accordingly."""
        names = self.classes()
        if class_name not in names:
            return self.stats()
        target = names.index(class_name)
        for lbl in list(self.labels.glob("*.txt")):
            out = []
            for line in lbl.read_text().splitlines():
                p = line.split()
                if not p:
                    continue
                cid = int(float(p[0]))
                if cid == target:
                    continue                       # drop this class's boxes
                if cid > target:
                    p[0] = str(cid - 1)            # shift higher indices down
                out.append(" ".join(p))
            if not out:                            # image had only this class
                (self.images / f"{lbl.stem}.jpg").unlink(missing_ok=True)
                lbl.unlink(missing_ok=True)
            else:
                lbl.write_text("\n".join(out) + "\n")
        nd = self.negatives / self._safe(class_name)
        if nd.exists():
            shutil.rmtree(nd)
        names.remove(class_name)
        self._write_classes(names)
        return self.stats()

    def clear(self):
        """Delete ALL images, labels and negatives, and reset the class list."""
        for d in (self.images, self.labels, self.negatives):
            if d.exists():
                shutil.rmtree(d)
        self.classes_file.write_text("")
        (self.root / "data.yaml").unlink(missing_ok=True)
        self.ensure()
        return self.stats()

    MIN_PER_CLASS = 15      # below this a class is flagged as too few

    def health(self):
        """Pre-train sanity check: per-class counts, unlabeled images, imbalance."""
        samples = self.list_samples()
        counts = {}
        for s in samples:
            for b in s["boxes"]:
                counts[b["className"]] = counts.get(b["className"], 0) + 1
        labeled = {l.stem for l in self.labels.glob("*.txt") if l.read_text().strip()}
        unlabeled = sum(1 for img in self.images.glob("*.jpg") if img.stem not in labeled)
        warnings = []
        for c in self.classes():
            n = counts.get(c, 0)
            if n == 0:
                warnings.append(f"'{c}' has no labeled images yet.")
            elif n < self.MIN_PER_CLASS:
                warnings.append(f"'{c}' has only {n} image(s) — aim for {self.MIN_PER_CLASS}+ for a reliable model.")
        if len(counts) > 1:
            mx, mn = max(counts.values()), min(counts.values())
            if mn > 0 and mx / mn >= 3:
                warnings.append(f"Class imbalance — most-labeled has {mx}, least has {mn}; add images to the smaller class(es).")
        if unlabeled:
            warnings.append(f"{unlabeled} image(s) have no label; they'll be ignored in training.")
        return {"per_class": counts, "unlabeled": unlabeled,
                "min_recommended": self.MIN_PER_CLASS, "warnings": warnings}

    def stats(self):
        samples = self.list_samples()
        counts = {}
        for s in samples:
            for b in s["boxes"]:
                counts[b["className"]] = counts.get(b["className"], 0) + 1
        negs = {}
        for n in self.list_negatives():
            negs[n["className"]] = negs.get(n["className"], 0) + 1
        return {"images": len(samples), "per_class": counts,
                "negatives": negs, "classes": self.classes()}
