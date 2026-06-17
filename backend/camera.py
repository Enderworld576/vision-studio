"""Camera layer — pluggable sources behind one interface.

Supported camera kinds:
  - "oak"  : OAK 4 (RVC4) over Ethernet via DepthAI (the default)
  - "usb"  : a local webcam / V4L2 device, by index (OpenCV)
  - "ip"   : an RTSP/HTTP(MJPEG) stream, by URL (OpenCV)

All kinds expose the same interface (start/stop/snapshot/mjpeg/status), so the
rest of the app doesn't care which camera is connected. Every camera runs a
background thread that keeps the latest JPEG frame ready; sources fail soft (a
bad camera sets `error` and the app keeps running).

OAK connection note: RVC4 over IP needs an explicit gate DeviceInfo with
X_LINK_GATE + X_LINK_RVC4 (NOT GATE_BOOTED, which reports "already used").
"""

import threading
import time

import cv2
import numpy as np


class BaseCamera:
    kind = "base"

    def __init__(self, source, fps=15, label=None):
        self.source = source
        self.fps = fps
        self.label = label or f"{self.kind}:{source}"
        self._cond = threading.Condition()
        self._jpeg = None
        self._count = 0
        self._running = False
        self._error = None
        self._thread = None

    # subclasses implement a generator that yields JPEG bytes until stopped
    def _frames(self):
        raise NotImplementedError

    def _interrupt(self):
        """Force a blocking grab to return so the loop can exit (optional)."""

    # -- lifecycle ----------------------------------------------------------
    def start(self):
        if self._running:
            return
        self._running = True
        self._error = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        try:
            for jpeg in self._frames():
                if not self._running:
                    break
                with self._cond:
                    self._jpeg = jpeg
                    self._count += 1
                    self._cond.notify_all()
        except Exception as e:
            self._error = str(e)
        finally:
            self._running = False
            with self._cond:
                self._cond.notify_all()

    def stop(self):
        self._running = False
        try:
            self._interrupt()
        except Exception:
            pass
        if self._thread:
            self._thread.join(timeout=4)
        self._thread = None
        with self._cond:
            self._jpeg = None
            self._count = 0

    # -- access -------------------------------------------------------------
    def snapshot(self, timeout=5.0):
        with self._cond:
            if self._jpeg is None:
                self._cond.wait(timeout=timeout)
            return self._jpeg

    def mjpeg(self):
        last = -1
        while True:
            with self._cond:
                while (self._count == last or self._jpeg is None):
                    if not self._running:
                        return
                    self._cond.wait(timeout=2.0)
                jpeg = self._jpeg
                last = self._count
            yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                   + str(len(jpeg)).encode() + b"\r\n\r\n" + jpeg + b"\r\n")

    def status(self):
        return {
            "kind": self.kind, "source": self.source, "label": self.label,
            "streaming": self._jpeg is not None and self._running,
            "running": self._running, "frames": self._count,
            "error": self._error, "fps": self.fps,
        }


class OakCamera(BaseCamera):
    kind = "oak"

    def __init__(self, source="169.254.155.80", fps=15, width=1920, height=1080,
                 quality=90, label=None):
        super().__init__(source, fps, label or f"OAK 4 ({source})")
        self.width, self.height, self.quality = width, height, quality
        self._pipeline = None
        self._device = None

    def _open(self):
        import depthai as dai
        if not self.source or self.source == "auto":
            return dai.Device()
        info = dai.DeviceInfo(
            str(self.source), "",
            dai.XLinkDeviceState.X_LINK_GATE,
            dai.XLinkProtocol.X_LINK_TCP_IP,
            dai.XLinkPlatform.X_LINK_RVC4,
            dai.XLinkError_t.X_LINK_SUCCESS,
        )
        return dai.Device(info)

    def _frames(self):
        import depthai as dai
        dev = self._open()
        self._device = dev
        pipeline = dai.Pipeline(dev)
        self._pipeline = pipeline
        cam = pipeline.create(dai.node.Camera).build(dai.CameraBoardSocket.CAM_A)
        out = cam.requestOutput((self.width, self.height),
                                type=dai.ImgFrame.Type.NV12, fps=self.fps)
        enc = pipeline.create(dai.node.VideoEncoder)
        enc.setDefaultProfilePreset(self.fps, dai.VideoEncoderProperties.Profile.MJPEG)
        enc.setQuality(self.quality)
        out.link(enc.input)
        q = enc.out.createOutputQueue()
        pipeline.start()
        while self._running and pipeline.isRunning():
            frame = q.get()
            data = frame.getData()
            yield data.tobytes() if hasattr(data, "tobytes") else bytes(data)

    def _interrupt(self):
        if self._pipeline is not None:
            self._pipeline.stop()
        if self._device is not None:
            self._device.close()
        self._pipeline = self._device = None


class CvCamera(BaseCamera):
    """USB webcam (integer index) or IP/RTSP/HTTP stream (URL) via OpenCV."""

    def __init__(self, source, fps=15, kind="usb", width=1280, height=720,
                 quality=85, label=None):
        self.kind = kind
        nice = f"USB camera {source}" if kind == "usb" else f"Stream ({source})"
        super().__init__(source, fps, label or nice)
        self.width, self.height, self.quality = width, height, quality
        self._cap = None

    def _frames(self):
        src = int(self.source) if str(self.source).isdigit() else self.source
        cap = cv2.VideoCapture(src)
        self._cap = cap
        if not cap.isOpened():
            raise RuntimeError(f"could not open camera source {self.source!r}")
        if self.kind == "usb":
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        interval = 1.0 / max(1, self.fps)
        params = [cv2.IMWRITE_JPEG_QUALITY, self.quality]
        while self._running:
            ok, frame = cap.read()
            if not ok:
                time.sleep(0.05)
                continue
            ok, enc = cv2.imencode(".jpg", frame, params)
            if ok:
                yield enc.tobytes()
            time.sleep(interval)

    def _interrupt(self):
        if self._cap is not None:
            self._cap.release()
            self._cap = None


def make_camera(kind, source, fps=15):
    kind = (kind or "oak").lower()
    if kind == "oak":
        return OakCamera(source=source or "169.254.155.80", fps=fps)
    if kind in ("usb", "ip"):
        return CvCamera(source=source, fps=fps, kind=kind)
    raise ValueError(f"unknown camera kind {kind!r}")


def discover_cameras(oak_ip="169.254.155.80", probe_usb=4):
    """Best-effort list of cameras the app could connect to.

    Always offers the configured OAK (over IP) and any auto-discovered OAKs;
    probes a few USB indices. IP/RTSP cameras are added manually in the UI.
    """
    found = [{"kind": "oak", "source": oak_ip, "label": f"OAK 4 ({oak_ip})"}]
    # USB-attached OAKs (auto-discovery works over USB, not IP)
    try:
        import depthai as dai
        for d in dai.Device.getAllAvailableDevices():
            mx = getattr(d, "deviceId", getattr(d, "mxid", "?"))
            found.append({"kind": "oak", "source": "auto",
                          "label": f"OAK (USB {mx})"})
    except Exception:
        pass
    # local webcams
    for i in range(probe_usb):
        cap = cv2.VideoCapture(i)
        ok = cap.isOpened() and cap.read()[0]
        cap.release()
        if ok:
            found.append({"kind": "usb", "source": i, "label": f"USB camera {i}"})
    return found
