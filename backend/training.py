"""Model training, run in a background thread with live progress.

Builds a single-class or multi-class train/val split from the flat dataset with
a real held-out validation set (~15-20%, never overlapping train), trains
YOLO11 (size n/s/m) on GPU if available else CPU, and on success publishes the
best weights as the active model. After training it captures mAP50 / mAP50-95
and copies a few held-out validation images with predicted boxes so the user
can see real performance. The UI polls `status()` for progress, metrics, device,
split sizes, and the rolling log.
"""

import math
import random
import shutil
import threading
import traceback
from pathlib import Path

import cv2
import torch
from ultralytics import YOLO

import orient_template as otmpl

VAL_FRAC = 0.2    # held-out validation fraction; train and val never overlap
MODEL_SIZES = ("n", "s", "m")


class Trainer:
    def __init__(self, dataset, models_dir, work_dir):
        self.dataset = dataset
        self.models_dir = Path(models_dir)
        self.work_dir = Path(work_dir)
        self.models_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._reset()

    def _reset(self):
        self.state = "idle"      # idle | preparing | training | done | error
        self.epoch = 0
        self.total = 0
        self.map50 = None
        self.map5095 = None
        self.message = ""
        self.log = []
        self.class_name = None
        self.weights = None
        self.previews = []       # val images with predicted boxes (file paths)
        self.device = "cpu"
        self.n_train = 0
        self.n_val = 0

    @property
    def busy(self):
        return self.state in ("preparing", "training")

    def _say(self, msg):
        self.log.append(msg)
        self.log = self.log[-200:]
        self.message = msg

    # -- dataset split ------------------------------------------------------
    def _prepare_split(self, class_name, val_frac, seed=42, dataset_negatives=True):
        """Build a single-class DETECTION train/val split (box only). Images that
        contain ONLY other labeled parts become background negatives when
        dataset_negatives is on, plus any manually-added negatives."""
        names = self.dataset.classes()
        if class_name not in names:
            raise ValueError(f"class {class_name!r} not in dataset")
        target = names.index(class_name)
        samples, has_kpts, ds_negs = [], False, []
        for lbl in sorted(self.dataset.labels.glob("*.txt")):
            img = self.dataset.images / f"{lbl.stem}.jpg"
            if not img.exists():
                continue
            kept, other = [], False
            for l in lbl.read_text().splitlines():
                p = l.split()
                if not p:
                    continue
                if int(float(p[0])) == target:
                    if len(p) >= 11:          # has front/back keypoints
                        has_kpts = True
                    kept.append("0 " + " ".join(p[1:5]))   # box only (detector)
                else:
                    other = True
            if kept:
                samples.append((img, kept))
            elif other:                        # only other parts -> dataset negative
                ds_negs.append(img)
        if len(samples) < 2:
            raise ValueError(f"need >=2 labeled images of {class_name!r}, have {len(samples)}")
        random.Random(seed).shuffle(samples)
        n_val = max(1, round(len(samples) * val_frac))
        splits = {"val": samples[:n_val], "train": samples[n_val:]}

        out = self.work_dir / "split"
        if out.exists():
            shutil.rmtree(out)
        for split, items in splits.items():
            (out / "images" / split).mkdir(parents=True, exist_ok=True)
            (out / "labels" / split).mkdir(parents=True, exist_ok=True)
            for img, kept in items:
                shutil.copy2(img, out / "images" / split / img.name)
                (out / "labels" / split / f"{img.stem}.txt").write_text("\n".join(kept) + "\n")
        # Negatives = background images (empty label) so the model rejects them:
        # other labeled parts in the dataset (if enabled) + any manual negatives.
        negs = (ds_negs if dataset_negatives else []) + self.dataset.negatives_for(class_name)
        if negs:
            random.Random(seed + 1).shuffle(negs)
            nv = round(len(negs) * val_frac)
            for split, imgs in {"val": negs[:nv], "train": negs[nv:]}.items():
                for img in imgs:
                    if (out / "images" / split / img.name).exists():
                        continue                      # don't clobber a positive
                    shutil.copy2(img, out / "images" / split / img.name)
                    (out / "labels" / split / f"{img.stem}.txt").write_text("")
            self._say(f"+ {len(negs)} negative (background) images"
                      + (f" ({len(ds_negs)} from other dataset parts)"
                         if dataset_negatives and ds_negs else ""))

        (out / "data.yaml").write_text(
            f"path: {out}\ntrain: images/train\nval: images/val\nnc: 1\nnames: ['{class_name}']\n")
        self._say(f"prepared {len(splits['train'])} train / {len(splits['val'])} val"
                  + (" (+ orientation labels)" if has_kpts else ""))
        return out / "data.yaml", has_kpts, len(splits["train"]), len(splits["val"])

    # -- run ----------------------------------------------------------------
    def start(self, class_name, epochs=150, imgsz=640, dataset_negatives=True,
              multiclass=False, model_size="n"):
        size = model_size if model_size in MODEL_SIZES else "n"
        with self._lock:
            if self.busy:
                raise RuntimeError("training already in progress")
            self._reset()
            self.state = "preparing"
            self.class_name = "all parts" if multiclass else class_name
            self.total = epochs
        threading.Thread(target=self._run,
                         args=(class_name, epochs, imgsz, dataset_negatives, multiclass, size),
                         daemon=True).start()

    def _run(self, class_name, epochs, imgsz, dataset_negatives=True,
             multiclass=False, model_size="n"):
        try:
            if multiclass:
                yaml, kpt_classes, n_train, n_val = self._prepare_multi(VAL_FRAC, dataset_negatives)
                model_name = "all-parts.pt"
            else:
                yaml, has_kpts, n_train, n_val = self._prepare_split(
                    class_name, VAL_FRAC, dataset_negatives=dataset_negatives)
                kpt_classes = {class_name} if has_kpts else set()
                model_name = f"{class_name}.pt"
            self.n_train, self.n_val = n_train, n_val
            self.device = "cuda" if torch.cuda.is_available() else "cpu"
            self.state = "training"
            self._say(f"training yolo11{model_size} on {self.device.upper()} "
                      f"({n_train} train / {n_val} held-out val)…")
            # Load the base weights from a WRITABLE location. Ultralytics
            # downloads a bare "yolo11n.pt" into the current working directory,
            # which in a packaged app is the read-only app bundle — so give it
            # an absolute path under work_dir (matches what first-run setup
            # pre-downloads, so it's reused rather than re-fetched).
            self.work_dir.mkdir(parents=True, exist_ok=True)
            base_weights = self.work_dir / f"yolo11{model_size}.pt"
            model = YOLO(str(base_weights))

            def on_epoch(trainer):
                self.epoch = int(trainer.epoch) + 1
                m = getattr(trainer, "metrics", {}) or {}
                for k in ("metrics/mAP50(B)", "metrics/mAP50"):
                    if k in m:
                        self.map50 = round(float(m[k]), 3)
                for k in ("metrics/mAP50-95(B)", "metrics/mAP50-95"):
                    if k in m:
                        self.map5095 = round(float(m[k]), 3)
                self._say(f"epoch {self.epoch}/{self.total}"
                          + (f"  mAP50={self.map50}" if self.map50 is not None else ""))

            model.add_callback("on_fit_epoch_end", on_epoch)
            run_dir = self.work_dir / "runs" / "train"
            model.train(
                data=str(yaml), epochs=epochs, imgsz=imgsz, batch=8,
                patience=40, device=(0 if self.device == "cuda" else "cpu"),
                project=str(self.work_dir / "runs"), name="train", exist_ok=True,
                verbose=False, degrees=180, translate=0.1, scale=0.5, fliplr=0.5,
                flipud=0.5, hsv_h=0.015, hsv_s=0.7, hsv_v=0.4, mosaic=1.0,
            )
            best = run_dir / "weights" / "best.pt"
            dest = self.models_dir / model_name
            shutil.copy2(best, dest)
            self.weights = str(dest)
            if kpt_classes:               # per-class orientation references
                self._save_templates(kpt_classes, dest)
            self._collect_previews(run_dir)
            self.state = "done"
            self._say(f"done — {dest.name}"
                      + (f"  ·  mAP50 {self.map50}" if self.map50 is not None else "")
                      + (f", mAP50-95 {self.map5095}" if self.map5095 is not None else ""))
        except Exception as e:
            self.state = "error"
            self._say(f"error: {e}")
            self.log.append(traceback.format_exc()[-1000:])

    def _collect_previews(self, run_dir):
        """Copy a few held-out validation images WITH predicted boxes (saved by
        Ultralytics as val_batch*_pred.jpg) so the user can see real performance."""
        preview_dir = self.work_dir / "preview"
        if preview_dir.exists():
            shutil.rmtree(preview_dir)
        preview_dir.mkdir(parents=True, exist_ok=True)
        self.previews = []
        for src in sorted(run_dir.glob("val_batch*_pred.jpg"))[:3]:
            dst = preview_dir / src.name
            shutil.copy2(src, dst)
            self.previews.append(str(dst))

    def _orient_items(self, class_name):
        """(bgr_img, box_px, front_deg) for each keypoint-labeled box of a class."""
        names = self.dataset.classes()
        target = names.index(class_name)
        items = []
        for lbl in self.dataset.labels.glob("*.txt"):
            img_p = self.dataset.images / f"{lbl.stem}.jpg"
            if not img_p.exists():
                continue
            for line in lbl.read_text().splitlines():
                p = line.split()
                if not p or int(float(p[0])) != target or len(p) < 11:
                    continue
                img = cv2.imread(str(img_p))
                if img is None:
                    continue
                H, W = img.shape[:2]
                cx, cy, w, h = float(p[1]), float(p[2]), float(p[3]), float(p[4])
                box = ((cx - w / 2) * W, (cy - h / 2) * H, (cx + w / 2) * W, (cy + h / 2) * H)
                fd = math.degrees(math.atan2((float(p[6]) - float(p[9])) * H,
                                             (float(p[5]) - float(p[8])) * W)) % 360
                items.append((img, box, fd))
        return items

    def _save_templates(self, class_names, model_path):
        """Build a per-class orientation reference into <model>.orient/<class>.npz."""
        odir = model_path.with_name(model_path.stem + ".orient")
        odir.mkdir(parents=True, exist_ok=True)
        for cn in class_names:
            ref = otmpl.build_reference(self._orient_items(cn))
            if ref is not None:
                otmpl.save(odir / f"{otmpl.safe_name(cn)}.npz", ref)
                self._say(f"built orientation reference for '{cn}'")

    def _prepare_multi(self, val_frac, dataset_negatives, seed=42):
        """Multi-class split: keep ALL classes with their original indices.
        Returns (yaml, classes_with_keypoints, n_train, n_val)."""
        names = self.dataset.classes()
        if not names:
            raise ValueError("no classes labeled yet")
        samples, kpt_classes = [], set()
        for lbl in sorted(self.dataset.labels.glob("*.txt")):
            img = self.dataset.images / f"{lbl.stem}.jpg"
            if not img.exists():
                continue
            kept = []
            for l in lbl.read_text().splitlines():
                p = l.split()
                if not p:
                    continue
                cid = int(float(p[0]))
                if cid >= len(names):
                    continue
                if len(p) >= 11:
                    kpt_classes.add(names[cid])
                kept.append(f"{cid} " + " ".join(p[1:5]))   # original index, box only
            if kept:
                samples.append((img, kept))
        if len(samples) < 2:
            raise ValueError(f"need >=2 labeled images, have {len(samples)}")
        random.Random(seed).shuffle(samples)
        n_val = max(1, round(len(samples) * val_frac))
        splits = {"val": samples[:n_val], "train": samples[n_val:]}
        out = self.work_dir / "split"
        if out.exists():
            shutil.rmtree(out)
        for split, items in splits.items():
            (out / "images" / split).mkdir(parents=True, exist_ok=True)
            (out / "labels" / split).mkdir(parents=True, exist_ok=True)
            for img, kept in items:
                shutil.copy2(img, out / "images" / split / img.name)
                (out / "labels" / split / f"{img.stem}.txt").write_text("\n".join(kept) + "\n")
        # In a multi-class model every labeled part is a POSITIVE (its own class),
        # so there are no "other parts as negatives" — the classes themselves
        # teach the model to distinguish them. Manually-added negative-example
        # images ("not any of these parts") are still useful background, so they
        # are always included here (the dataset_negatives flag does not apply).
        negs = [p for cn in names for p in self.dataset.negatives_for(cn)]
        if negs:
            random.Random(seed + 1).shuffle(negs)
            nv = round(len(negs) * val_frac)
            for split, imgs in {"val": negs[:nv], "train": negs[nv:]}.items():
                for img in imgs:
                    if (out / "images" / split / img.name).exists():
                        continue
                    shutil.copy2(img, out / "images" / split / img.name)
                    (out / "labels" / split / f"{img.stem}.txt").write_text("")
            self._say(f"+ {len(negs)} manual negative (background) images")
        nm = ", ".join(repr(n) for n in names)
        (out / "data.yaml").write_text(
            f"path: {out}\ntrain: images/train\nval: images/val\nnc: {len(names)}\nnames: [{nm}]\n")
        self._say(f"prepared {len(splits['train'])} train / {len(splits['val'])} val "
                  f"across {len(names)} classes"
                  + (" (+ orientation labels)" if kpt_classes else ""))
        return out / "data.yaml", kpt_classes, len(splits["train"]), len(splits["val"])

    def status(self):
        return {
            "state": self.state, "epoch": self.epoch, "total": self.total,
            "map50": self.map50, "map5095": self.map5095, "message": self.message,
            "class_name": self.class_name, "weights": self.weights,
            "device": self.device, "n_train": self.n_train, "n_val": self.n_val,
            "has_preview": bool(self.previews), "log": self.log[-30:],
        }
