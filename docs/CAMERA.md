# Camera notes

Vision Studio works with several camera types. The **OAK 4** is the default;
you can switch to a USB webcam or an IP/RTSP stream from the **Home → Camera**
panel (click **Scan** to list what's attached, or enter an address).

| Kind | What it is | Source you enter |
| --- | --- | --- |
| `oak` | OAK 4 (RVC4) over Ethernet — **default** | IP, e.g. `169.254.1.222` |
| `usb` | local webcam / V4L2 device | an index, e.g. `0` |
| `ip`  | RTSP or HTTP(MJPEG) network camera | a URL, e.g. `rtsp://…` |

USB and IP cameras use OpenCV; the OAK uses DepthAI. All of them present the
same live feed + snapshot to the rest of the app, so detection and training work
identically regardless of which camera is connected.

The OAK 4 specifics below are baked into `backend/camera.py`.

## Connecting

- Default address: **`169.254.1.222`** (link-local). Set yours on the Home
  screen and click **Reconnect**, or pass `--device <ip>` to the backend.
- The camera is reached through the on-device **gate** (port 9998). Auto-discovery
  and a plain `DeviceInfo(ip)` do **not** work over IP.
- The working `DeviceInfo` uses **`X_LINK_GATE` + `X_LINK_RVC4`**. (Using
  `X_LINK_GATE_BOOTED` instead always reports *"Device is already used"* even on
  a freshly rebooted device — it attaches to an existing session rather than
  opening a fresh one. `X_LINK_ANY_PLATFORM` is rejected by the gate.)

## Single-client

The OAK serves **one client at a time**. If the Luxonis **OAK Viewer** app (or
any other DepthAI program) is connected, Vision Studio can't get the camera, and
you'll see *"Device is already used."* Close the other app and click Reconnect.

Vision Studio releases the camera cleanly on exit, so it won't leave a stuck
session behind.

## Power

The OAK 4 needs **PoE+ (802.3at, ~30 W)** — plain PoE (15 W) isn't enough and the
imaging pipeline browns out (the device still pings/SSHes, but won't stream).

## No camera? Still works

You can use **Test → Upload image…** and train on imported images without a
camera connected. The live feed and snapshot features simply stay disabled.
