"""Turret server.  Run: python laptop/app.py   then open http://localhost:8000

Relays the Pi's JPEG frames to the browser. Click a target in the video to lock its color (a box appears).
When tracking is switched on from the dashboard the server also follows the clicked target and nudges the gimbal (ESP32 on COM7) to center it. Tracking starts OFF.
Binds to localhost only.
"""
import ctypes
import json
import math
import os
import statistics
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from aim import Aimer              # noqa: E402
from engage import Engagement     # noqa: E402
from gimbal import AsyncGimbal     # noqa: E402
from predict import LeadPredictor  # noqa: E402
from receiver import CameraFeed, FrameFeed   # noqa: E402
from telemetry import Telemetry    # noqa: E402
from depth_nn import NeuralDepth, overlay as depth_overlay   # noqa: E402  (loads onnxruntime only when the layer is first switched on)
from tracker import PRESETS, ColorTracker   # noqa: E402

# "COM7" for USB serial, or e.g. "udp:192.168.137.50" for the Wi-Fi firmware. Override: set GIMBAL=udp:...
GIMBAL_PORT = os.environ.get("GIMBAL", "COM7")
# Video source: the Pi by default. `python laptop/app.py --camera 1` (or CAMERA=1) uses a USB webcam on this laptop instead.
CAMERA = (int(sys.argv[sys.argv.index("--camera") + 1]) if "--camera" in sys.argv
          else int(os.environ["CAMERA"]) if os.environ.get("CAMERA", "").isdigit() else None)
CLASSIC = open(os.path.join(HERE, "dashboard.html"), encoding="utf-8").read().encode()   # the previous dashboard, at /classic
# The pages and assets of the current dashboard (laptop/ui/). Only these names are served, so no other file can be read.
UI_FILES = {"/": ("live.html", "text/html; charset=utf-8"), "/diagnostics": ("diagnostics.html", "text/html; charset=utf-8"),
            "/ui.css": ("ui.css", "text/css; charset=utf-8"), "/ui.js": ("ui.js", "application/javascript; charset=utf-8")}
STARTED = time.time()
BOOT = time.time()                  # lets the page detect a server restart and reconnect its video

new_frame = threading.Condition()   # notified whenever `latest` changes
latest = {"jpg": None, "seq": 0, "state": {"connected": False, "fps": 0, "kb": 0, "boot": BOOT}}

tracker = ColorTracker()
# This turret's servo directions (+1 = angle grows as the target goes right / down in the picture). Tilt is -1: tested
# on the real turret, the other way pointed the wrong direction. The dashboard's Flip buttons change these live.
DEFAULT_SIGNS = {"pan": 1, "tilt": -1}
view = {"mirror": True, "vflip": False, "mask": False, "depth": False, "box": True, "trail": False}   # flip the picture so it matches the real world (set from the dashboard)
cfg = {"lead_ms": 0}                 # aim this far ahead of a moving target; 0 = off (it amplifies noise: raise only
                                     # to about the measured camera-to-servo delay, using the dashboard slider)
predictor = LeadPredictor()
engage = Engagement()                 # SEARCH/ACQUIRE/TRACK/LOCK/COOLDOWN and the fire interlocks (simulated emitter)
telemetry = Telemetry()
info = {"speed": 0.0, "settled": False}   # target speed (px/s) and whether the gimbal is on target, per frame
ctl = {"tracking": False, "aimer": None, "error": None, "angles": None}
ctl_lock = threading.Lock()


def set_tracking(on):
    """Switch tracking on/off; opens the gimbal on first use. Returns an error string or None."""
    with ctl_lock:
        if not on:
            ctl["tracking"] = False
            engage.arm(False, time.time())     # switching tracking off always disarms
            return None
        try:
            if ctl["aimer"] is None:
                ctl["aimer"] = Aimer(AsyncGimbal(GIMBAL_PORT))
                ctl["aimer"].sign.update(DEFAULT_SIGNS)
            ctl["angles"] = ctl["aimer"].gimbal.status()
            ctl["tracking"], ctl["error"] = True, None
        except Exception as e:          # port busy / ESP unplugged / bad reply
            ctl["tracking"], ctl["error"] = False, f"gimbal: {e}"
        return ctl["error"]


depth_state = {"net": None, "error": None}     # the neural depth layer (off by default)
trail = deque(maxlen=45)                       # recent target positions, for the Trail layer
trail_seen = [0.0]                             # when the target was last seen (the trail clears after a second without it)


def apply_depth(frame):
    """If the Depth layer is on: hand the clean picture to the background network and lay its newest result over it.
    The network runs on its own thread, so this never waits. If it cannot start, the layer switches itself off."""
    if not view["depth"]:
        return frame
    if depth_state["net"] is None:
        depth_state["net"] = NeuralDepth()
    net = depth_state["net"]
    if not net.available:
        depth_state["error"], view["depth"] = net.error, False
        return frame
    depth_state["error"] = None
    net.submit(frame)
    result = net.latest()
    return depth_overlay(frame, result[0]) if result else frame


def process(jpg, ts=None):
    """Apply the view flips and, if a target is armed, find it, aim, and draw. Returns (JPEG, found flag)."""
    now = time.time() if ts is None else ts
    if not (view["mirror"] or view["vflip"] or view["depth"] or tracker.armed):
        engage.update(now, False, False, False)
        return jpg, False                # nothing to change: the Pi's JPEG goes straight to the browser
    frame = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
    if view["mirror"] or view["vflip"]:
        frame = cv2.flip(frame, -1 if view["mirror"] and view["vflip"] else 1 if view["mirror"] else 0)
    found = False
    face = tracker.find(frame) if tracker.armed else None     # find the target on the CLEAN picture, before any overlay
    frame = apply_depth(frame)
    if tracker.armed:
        h, w = frame.shape[:2]
        if view["mask"] and tracker.debug_mask is not None:   # tint every pixel the color filter accepts
            hit = cv2.resize(tracker.debug_mask, (w, h), interpolation=cv2.INTER_NEAREST) > 0
            frame[hit] = (0.4 * frame[hit] + 0.6 * np.array((255, 0, 255))).astype(np.uint8)
        if view["box"]:
            cv2.drawMarker(frame, (w // 2, h // 2), (255, 255, 255), cv2.MARKER_CROSS, 20, 1)
        if face:
            found = True
            cx, cy, fw, fh = face
            trail.append((int(cx), int(cy)))
            trail_seen[0] = now
            if view["box"]:
                cv2.rectangle(frame, (int(cx - fw / 2), int(cy - fh / 2)), (int(cx + fw / 2), int(cy + fh / 2)), (0, 255, 0), 2)
            ax, ay = cx, cy                    # the point to aim at: the target, or where it is heading
            if cfg["lead_ms"] > 0:
                ax, ay = predictor.update(now, cx, cy, cfg["lead_ms"] / 1000, (w, h))
                if view["box"]:
                    cv2.circle(frame, (int(ax), int(ay)), 6, (0, 255, 255), -1)
            else:
                predictor.reset()
            try:
                with ctl_lock:
                    if ctl["tracking"]:
                        ctl["angles"] = ctl["aimer"].update((ax - w / 2) / (w / 2), (ay - h / 2) / (h / 2))
                        info["settled"] = ctl["aimer"].settled
            except Exception as e:
                with ctl_lock:
                    ctl["tracking"], ctl["error"] = False, f"gimbal: {e}"
            info["speed"] = math.hypot(*predictor.velocity)
    if not found and trail and now - trail_seen[0] > 1.0:
        trail.clear()
    if view["trail"] and len(trail) > 1:           # older points are thinner and dimmer
        pts = list(trail)
        for i in range(1, len(pts)):
            k = i / len(pts)
            cv2.line(frame, pts[i - 1], pts[i], (int(60 + 195 * k), int(180 + 60 * k), int(40 + 120 * k)), 1 + int(3 * k), cv2.LINE_AA)
    if not found:
        info.update(speed=0.0, settled=False)
        if ctl["aimer"]:
            ctl["aimer"].reset()
    tracking = ctl["tracking"] and tracker.armed
    _, scan_goal = engage.update(now, tracking, found, info["settled"], info["speed"])
    if scan_goal and tracking and ctl["aimer"]:      # auto-scan: sweep while searching
        with ctl_lock:
            ctl["angles"] = ctl["aimer"].gimbal.move_to(*scan_goal)
    return cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 90])[1].tobytes(), found


def capture_loop():
    feed = CameraFeed(CAMERA) if CAMERA is not None else FrameFeed()    # a laptop webcam, or the Pi's stream
    arrivals, count, seq, last_frame = [], 0, -1, time.time()
    while True:
        item = feed.get(seq)
        if item is None:                      # nothing from the Pi for a while
            if time.time() - last_frame > 4:
                latest["state"] = dict(latest["state"], connected=False, fps=0)
            continue
        seq, ts, jpg = item
        last_frame = time.time()
        age_ms = (last_frame - ts) * 1000     # how long the frame waited before we got to it
        t_proc = time.perf_counter()
        jpg, found = process(jpg, ts)
        proc_ms = (time.perf_counter() - t_proc) * 1000
        pan, tilt = ctl["angles"] or (None, None)
        telemetry.add(ts, age_ms=age_ms, proc_ms=proc_ms, pan=pan, tilt=tilt, speed=info["speed"])
        arrivals = [a for a in arrivals + [ts] if ts - a < 3]
        fps = (len(arrivals) - 1) / (arrivals[-1] - arrivals[0]) if len(arrivals) > 1 else 0
        count += 1
        aimer = ctl["aimer"]
        state = {"connected": True, "fps": round(fps, 1), "frames": count, "kb": round(len(jpg) / 1024),
                 "boot": BOOT, "tracking": ctl["tracking"], "target": found, "armed": tracker.armed,
                 "error": ctl["error"] or (aimer.gimbal.error if aimer else None),
                 "angles": ctl["angles"], "sign": aimer.sign if aimer else None,
                 "mode": aimer.mode if aimer else "map", "view": dict(view),
                 "source": f"webcam {CAMERA}" if CAMERA is not None else "Pi",
                 "depth": depth_state["net"].stats() if depth_state["net"] else None, "depth_error": depth_state["error"],
                 "dropped": feed.dropped, "age_ms": round(age_ms), "cfg": dict(cfg),
                 "engage": {"state": engage.state, "armed": engage.armed, "auto_scan": engage.auto_scan,
                            "shots": engage.shots, "max_shots": engage.max_shots}}
        with new_frame:
            latest.update(jpg=jpg, seq=count, state=state)
            new_frame.notify_all()


_cpu_mark = {"wall": time.time(), "cpu": time.process_time()}


def system_stats():
    """Memory and CPU use for the diagnostics page (Windows). Any value that cannot be read is None."""
    out = {"uptime_s": round(time.time() - STARTED), "ram_free_gb": None, "ram_total_gb": None, "process_mb": None,
           "cpu_process_pct": None, "python": sys.version.split()[0]}
    now, cpu = time.time(), time.process_time()
    if now > _cpu_mark["wall"]:
        out["cpu_process_pct"] = round(100 * (cpu - _cpu_mark["cpu"]) / (now - _cpu_mark["wall"]) / (os.cpu_count() or 1), 1)
    _cpu_mark.update(wall=now, cpu=cpu)
    try:
        class MemStatus(ctypes.Structure):
            _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong), ("total", ctypes.c_ulonglong),
                        ("avail", ctypes.c_ulonglong), ("tpage", ctypes.c_ulonglong), ("apage", ctypes.c_ulonglong),
                        ("tvirt", ctypes.c_ulonglong), ("avirt", ctypes.c_ulonglong), ("ext", ctypes.c_ulonglong)]
        m = MemStatus()
        m.length = ctypes.sizeof(MemStatus)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
        out["ram_free_gb"], out["ram_total_gb"] = round(m.avail / 2**30, 1), round(m.total / 2**30, 1)

        class ProcMem(ctypes.Structure):
            _fields_ = [("cb", ctypes.c_ulong), ("faults", ctypes.c_ulong), ("peak", ctypes.c_size_t), ("ws", ctypes.c_size_t),
                        ("a", ctypes.c_size_t), ("b", ctypes.c_size_t), ("c", ctypes.c_size_t), ("d", ctypes.c_size_t),
                        ("e", ctypes.c_size_t), ("f", ctypes.c_size_t)]
        p = ProcMem()
        p.cb = ctypes.sizeof(ProcMem)
        fn = ctypes.windll.psapi.GetProcessMemoryInfo
        fn.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
        fn(ctypes.c_void_p(-1), ctypes.byref(p), p.cb)     # -1 = this process
        out["process_mb"] = round(p.ws / 2**20)
    except (AttributeError, OSError):
        pass
    return out


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, body, ctype="application/json", code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in UI_FILES:
            name, ctype = UI_FILES[self.path]
            try:
                with open(os.path.join(HERE, "ui", name), "rb") as f:     # read on every request: edits show up on reload
                    self._send(f.read(), ctype)
            except OSError:
                self._send(b"missing UI file: " + name.encode(), "text/plain", 500)
        elif self.path == "/classic":
            self._send(CLASSIC, "text/html; charset=utf-8")
        elif self.path == "/system":
            self._send(json.dumps(system_stats()).encode())
        elif self.path == "/state":
            self._send(json.dumps(latest["state"]).encode())
        elif self.path == "/telemetry":
            snap, now = telemetry.snapshot(), time.time()
            snap["events"] = [[round(now - t, 1), text] for t, text in list(engage.events)[-14:]]
            rtt = list(ctl["aimer"].gimbal.rtt_ms) if ctl["aimer"] else []
            snap["servo_rtt_ms"] = round(statistics.median(rtt), 1) if rtt else None
            self._send(json.dumps(snap).encode())
        elif self.path.startswith("/frame.jpg"):
            self._frame()
        elif self.path.startswith("/stream"):
            self._stream()
        else:
            self._send(b"{}", code=404)

    def _frame(self):
        """Latest frame as a single JPEG. ?after=<seq> waits for a frame newer than <seq> (or different,
        e.g. after a server restart), so the page can poll at the camera's pace without any long-lived stream."""
        after = int(parse_qs(urlparse(self.path).query).get("after", ["-1"])[0])
        with new_frame:
            new_frame.wait_for(lambda: latest["seq"] != after and latest["jpg"] is not None, timeout=2)
            seq, jpg = latest["seq"], latest["jpg"]
        if jpg is None:
            return self._send(b"", code=204)
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(jpg)))
        self.send_header("X-Seq", str(seq))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(jpg)

    def _stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=f")
        self.end_headers()
        seen = 0
        try:
            while True:
                with new_frame:
                    new_frame.wait_for(lambda: latest["seq"] != seen, timeout=2)
                    seen, jpg = latest["seq"], latest["jpg"]
                if jpg:
                    self.wfile.write(b"--f\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(jpg)
                                     + jpg + b"\r\n")
        except OSError:
            pass    # browser closed the stream

    def do_POST(self):
        url = urlparse(self.path)
        q = parse_qs(url.query)
        if url.path == "/track":
            err = set_tracking(q.get("on", ["0"])[0] == "1")
            return self._send(json.dumps({"error": err}).encode())
        if url.path == "/select" and latest["jpg"]:
            frame = cv2.imdecode(np.frombuffer(latest["jpg"], np.uint8), cv2.IMREAD_COLOR)
            ok = tracker.select(frame, float(q["x"][0]), float(q["y"][0]))
            return self._send(json.dumps({"ok": ok}).encode())
        if url.path == "/preset" and q.get("name", [""])[0] in PRESETS:
            tracker.use_preset(q["name"][0])
            return self._send(b"{}")
        if url.path == "/view" and q.get("key", [""])[0] in view:
            view[q["key"][0]] = q.get("on", ["0"])[0] == "1"
            if q["key"][0] in ("mirror", "vflip"):       # the picture just changed orientation: forget the old position
                tracker.last = None
                predictor.reset()
                trail.clear()
            return self._send(b"{}")
        if url.path == "/arm":
            engage.arm(q.get("on", ["0"])[0] == "1", time.time())
            return self._send(b"{}")
        if url.path == "/autoscan":
            engage.auto_scan = q.get("on", ["0"])[0] == "1"
            return self._send(b"{}")
        if url.path == "/cfg" and "lead_ms" in q:
            cfg["lead_ms"] = max(0, min(800, int(float(q["lead_ms"][0]))))
            return self._send(b"{}")
        if url.path == "/clear":
            tracker.clear()
            return self._send(b"{}")
        if url.path == "/mode" and ctl["aimer"] and q.get("name", [""])[0] in ("map", "follow"):
            ctl["aimer"].set_mode(q["name"][0])
            return self._send(b"{}")
        if url.path == "/flip" and ctl["aimer"] and q.get("axis", [""])[0] in ("pan", "tilt"):
            ctl["aimer"].flip(q["axis"][0])
            return self._send(b"{}")
        if url.path == "/center" and ctl["aimer"]:
            with ctl_lock:
                ctl["angles"] = ctl["aimer"].gimbal.center()
            return self._send(b"{}")
        self._send(b"{}", code=404)


if __name__ == "__main__":
    threading.Thread(target=capture_loop, daemon=True).start()
    print("Turret: http://localhost:8000", flush=True)
    ThreadingHTTPServer(("127.0.0.1", 8000), Handler).serve_forever()
