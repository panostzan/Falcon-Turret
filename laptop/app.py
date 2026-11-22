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
from gimbal import TILT_RANGE, AsyncGimbal     # noqa: E402
from predict import LeadPredictor  # noqa: E402
from receiver import CameraFeed, FrameFeed   # noqa: E402
import tilt_calibration                        # noqa: E402
from telemetry import Telemetry    # noqa: E402
from depth_nn import DepthSmoother, NeuralDepth, thumbnail, overlay as depth_overlay   # noqa: E402  (loads onnxruntime only when the layer is first switched on)
from tracker import PRESETS, ColorTracker, LastSeen   # noqa: E402

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
# The tilt servo's usable travel (degrees). The picture height maps onto it: bottom of the picture = the high end (45), top = the low
# end (0), middle = 22.5. Override with TILT_WINDOW="0,45"; "full" uses the whole 90-180 range and the older sign.
_cal_window, _cal_sign = tilt_calibration.load()          # made with tools/tilt_watch.py (Mark TOP / Mark BOTTOM)
_tw = os.environ.get("TILT_WINDOW") or ("%d,%d" % _cal_window if _cal_window else "0,45")
TILT_WINDOW = None if _tw == "full" else tuple(int(v) for v in _tw.split(","))
DEFAULT_SIGNS = {"pan": 1, "tilt": (_cal_sign if (_cal_window and not os.environ.get("TILT_WINDOW")) else 1) if TILT_WINDOW else -1}
view = {"mirror": True, "vflip": False, "mask": False, "depth": False, "box": True, "trail": False, "smooth": True, "depthcheck": False, "cutout": False, "fadestale": False, "qnx": True}   # flip the picture so it matches the real world (set from the dashboard)
cfg = {"lead_ms": 0, "min_conf": 80, "cut": 15}                 # aim this far ahead of a moving target; 0 = off (it amplifies noise: raise only
                                     # to about the measured camera-to-servo delay, using the dashboard slider)
predictor = LeadPredictor()
engage = Engagement()                 # SEARCH/ACQUIRE/TRACK/LOCK/COOLDOWN and the fire interlocks (simulated emitter)
telemetry = Telemetry()
info = {"speed": 0.0, "settled": False}   # target speed (px/s) and whether the gimbal is on target, per frame
ctl = {"tracking": False, "aimer": None, "error": None, "angles": None}
ctl_lock = threading.Lock()


feed_ref = {"feed": None}         # the video source (set by capture_loop); the Pi's feed also carries the QNX side
qnx_sync = {"t": 0.0, "arm_cmd": 0.0}


def send_pi(line):
    """One control line to turretd (the tracker + fire interlock running on QNX). Does nothing without the Pi."""
    f = feed_ref["feed"]
    return f.send(line) if f else False


def qnx_status():
    """turretd's newest answer if the QNX tracker should be used right now, else None. It is used when the layer is on, a
    color preset is the target (the C tracker has presets, not click-to-pick) and the answer is recent."""
    f = feed_ref["feed"]
    if not (view["qnx"] and f and tracker.preset in ("teal", "blue")):
        return None
    return f.qnx_fresh()


def set_tracking(on):
    """Switch tracking on/off; opens the gimbal on first use. Returns an error string or None."""
    with ctl_lock:
        if not on:
            ctl["tracking"] = False
            engage.arm(False, time.time())     # switching tracking off always disarms
            send_pi("arm 0")
            send_pi("track 0")
            return None
        try:
            if ctl["aimer"] is None:
                ctl["aimer"] = Aimer(AsyncGimbal(GIMBAL_PORT, tilt_limits=TILT_WINDOW or TILT_RANGE), tilt_window=TILT_WINDOW)
                if TILT_WINDOW:
                    engage.scan_tilt = ctl["aimer"].tilt_center
                ctl["aimer"].sign.update(DEFAULT_SIGNS)
            ctl["angles"] = ctl["aimer"].gimbal.status()
            ctl["tracking"], ctl["error"] = True, None
            send_pi("track 1")
        except Exception as e:          # port busy / ESP unplugged / bad reply
            ctl["tracking"], ctl["error"] = False, f"gimbal: {e}"
        return ctl["error"]


depth_state = {"net": None, "error": None, "smoother": DepthSmoother(), "stale": None}     # the neural depth layer (off by default)
last_seen = LastSeen()                         # last confirmed position, for the ghost and the search status
trail = deque(maxlen=45)                       # recent target positions, for the Trail layer
trail_seen = [0.0]                             # when the target was last seen (the trail clears after a second without it)


def depth_map(frame):
    """If the Depth layer or the depth check is on: hand the clean picture to the background network and return its
    newest result (smoothed and motion-compensated unless smoothing is off), or None. The network runs on its own
    thread, so this never waits. If it cannot start, both switch themselves off."""
    if not (view["depth"] or view["depthcheck"] or view["cutout"]):
        return None
    if depth_state["net"] is None:
        depth_state["net"] = NeuralDepth()
    net = depth_state["net"]
    if not net.available:
        depth_state["error"], view["depth"], view["depthcheck"], view["cutout"] = net.error, False, False, False
        return None
    depth_state["error"] = None
    net.submit(frame)
    if not view["smooth"]:                                      # raw: the newest result as it is (steps at the network's rate)
        depth_state["stale"] = None
        result = net.latest()
        return result[0] if result else None
    full = net.latest_full()
    if not full:
        return None
    sm, now, thumb = depth_state["smoother"], time.time(), thumbnail(frame)
    sm.update(full[0], full[1], full[2], now)
    depth_state["stale"] = sm.stale_mask(thumb) if view["fadestale"] else None    # experimental, off by default
    return sm.current(now, thumb)


def qnx_summary():
    """What the dashboard shows about the QNX side: is it answering, and what does it decide."""
    f = feed_ref["feed"]
    m = f.qnx_fresh() if f else None
    out = {"present": m is not None, "used": qnx_status() is not None, "on": view["qnx"], "events": []}
    if m:
        out.update(state=m["state"], armed=m["armed"], shots=m["shots"], link=m["link"], proc_ms=round(m["proc_us"] / 1000, 1),
                   age_ms=round((time.time() - f.meta_at) * 1000))
        age_s = time.time() - f.meta_at                   # both clocks below are the Pi's, so the difference is exact
        out["events"] = [[round(max(0.0, (m["t_ms"] - t) / 1000.0) + age_s, 1), text] for t, text in f.events[-8:]]
    return out


def cutout_active():
    """The Cutout layer can only work while a target's depth is known (and the target was seen recently)."""
    return bool(view["cutout"] and tracker.depth_focus is not None and tracker.missed <= 10)


def apply_cutout(frame, dmap):
    """Dim everything clearly FARTHER than the target (things at its depth, or nearer, stay bright). Where the picture
    moved since the depth was computed the depth is stale, so nothing is dimmed there. `cfg["cut"]` is how much farther
    than the target (in percent of the depth range) something must be before it dims."""
    if dmap is None or not cutout_active():
        return frame
    h, w = frame.shape[:2]
    d = cv2.resize(dmap, (w, h), interpolation=cv2.INTER_LINEAR)
    far = np.clip((tracker.depth_focus - cfg["cut"] / 100.0 - d) / 0.08, 0.0, 1.0)
    if depth_state["stale"] is not None:
        far *= 1.0 - cv2.resize(depth_state["stale"], (w, h), interpolation=cv2.INTER_LINEAR)
    far = (0.85 * far).astype(np.float32)
    return cv2.blendLinear(frame, cv2.convertScaleAbs(frame, alpha=0.25), 1.0 - far, far)


def apply_depth(frame, dmap):
    """Lay the depth heatmap over the picture when the Depth layer is on."""
    return depth_overlay(frame, dmap, stale=depth_state["stale"]) if (view["depth"] and dmap is not None) else frame


LIME, AMBER = (58, 226, 182), (0, 176, 255)     # BGR


def draw_reticle(frame, cx, cy, fw, fh, conf, need):
    """Corner-bracket reticle around the target with a confidence bar. Lime = sure enough to engage,
    amber = not sure (the engagement logic will not lock or fire). The white tick is the required level."""
    col = LIME if conf >= need else AMBER
    x0, y0, x1, y1 = int(cx - fw / 2) - 6, int(cy - fh / 2) - 6, int(cx + fw / 2) + 6, int(cy + fh / 2) + 6
    L = max(10, int(min(x1 - x0, y1 - y0) * 0.28))
    for (px, py, dx, dy) in ((x0, y0, 1, 1), (x1, y0, -1, 1), (x0, y1, 1, -1), (x1, y1, -1, -1)):
        cv2.line(frame, (px, py), (px + dx * L, py), col, 2)
        cv2.line(frame, (px, py), (px, py + dy * L), col, 2)
    mx, my = (x0 + x1) // 2, (y0 + y1) // 2
    for a, b in (((mx, y0), (mx, y0 + 6)), ((mx, y1), (mx, y1 - 6)), ((x0, my), (x0 + 6, my)), ((x1, my), (x1 - 6, my))):
        cv2.line(frame, a, b, col, 1)
    cv2.circle(frame, (int(cx), int(cy)), 2, col, -1)
    bw = max(40, x1 - x0)
    by = min(frame.shape[0] - 10, y1 + 8)
    cv2.rectangle(frame, (x0, by), (x0 + bw, by + 4), (40, 48, 30), -1)
    cv2.rectangle(frame, (x0, by), (x0 + int(bw * conf), by + 4), col, -1)
    cv2.line(frame, (x0 + int(bw * need), by - 3), (x0 + int(bw * need), by + 7), (255, 255, 255), 1)
    cv2.putText(frame, f"{int(conf * 100)}%", (x0, max(14, y0 - 6)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, col, 1, cv2.LINE_AA)


def dashed(img, p0, p1, col, dash=6, thick=1):
    """A dashed line (OpenCV only draws solid ones)."""
    (x0, y0), (x1, y1) = p0, p1
    n = max(1, int(math.hypot(x1 - x0, y1 - y0) // dash))
    for i in range(0, n, 2):
        a, b = i / n, min(1.0, (i + 1) / n)
        cv2.line(img, (int(x0 + (x1 - x0) * a), int(y0 + (y1 - y0) * a)), (int(x0 + (x1 - x0) * b), int(y0 + (y1 - y0) * b)), col, thick)


def draw_ghost(frame, lost):
    """Fading dotted box at the last CONFIRMED position, and a separate dashed marker for the ESTIMATED position now."""
    cx, cy, w, h = lost["last"]
    fade = max(0.0, 1.0 - lost["ago"] / LastSeen.GHOST_S)
    layer = frame.copy()
    x0, y0, x1, y1 = cx - w // 2, cy - h // 2, cx + w // 2, cy + h // 2
    for a, b in (((x0, y0), (x1, y0)), ((x1, y0), (x1, y1)), ((x1, y1), (x0, y1)), ((x0, y1), (x0, y0))):
        dashed(layer, a, b, AMBER, 5, 2)
    cv2.putText(layer, f"last seen {lost['ago']}s ago", (x0, max(14, y0 - 6)), cv2.FONT_HERSHEY_SIMPLEX, 0.45, AMBER, 1, cv2.LINE_AA)
    if lost["pred"]:
        px, py = lost["pred"]
        dashed(layer, (cx, cy), (px, py), (255, 255, 255), 4, 1)
        cv2.circle(layer, (px, py), max(8, min(w, h) // 3), (255, 255, 255), 1, cv2.LINE_AA)
        cv2.line(layer, (px - 5, py), (px + 5, py), (255, 255, 255), 1)
        cv2.line(layer, (px, py - 5), (px, py + 5), (255, 255, 255), 1)
        cv2.putText(layer, "estimate", (px + 10, py - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (255, 255, 255), 1, cv2.LINE_AA)
    cv2.addWeighted(layer, fade, frame, 1 - fade, 0, dst=frame)


def process(jpg, ts=None):
    """Apply the view flips and, if a target is armed, find it, aim, and draw. Returns (JPEG, found flag)."""
    now = time.time() if ts is None else ts
    if not (view["mirror"] or view["vflip"] or view["depth"] or view["depthcheck"] or view["cutout"] or tracker.armed):
        engage.update(now, False, False, False)
        return jpg, False                # nothing to change: the Pi's JPEG goes straight to the browser
    frame = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
    if view["mirror"] or view["vflip"]:
        frame = cv2.flip(frame, -1 if view["mirror"] and view["vflip"] else 1 if view["mirror"] else 0)
    found = False
    qs = qnx_status() if tracker.armed else None
    if qs is not None:                                        # the answer came from the tracker running on QNX
        fh, fw = frame.shape[:2]
        face = None
        if qs["found"]:
            cx = fw - qs["cx"] if view["mirror"] else qs["cx"]
            cy = fh - qs["cy"] if view["vflip"] else qs["cy"]
            face = (cx, cy, qs["w"], qs["h"])
        tracker.confidence, tracker.conf_parts, tracker.blob = (qs["confidence"] if face else 0.0), {}, None
        if engage.armed and not qs["armed"] and time.time() - qnx_sync["arm_cmd"] > 1.5:
            engage.arm(False, now)                            # QNX disarmed (watchdog or its own rules): follow it
    else:
        face = tracker.find(frame) if tracker.armed else None  # find the target on the CLEAN picture, before any overlay
    if time.time() - qnx_sync["t"] > 1.0:                     # keep the Pi's idea of the target and tracking in step
        qnx_sync["t"] = time.time()
        send_pi(f"preset {tracker.preset}" if tracker.preset in ("teal", "blue") else "clear")
        send_pi(f"track {1 if ctl['tracking'] else 0}")
        send_pi(f"min_conf {int(engage.min_confidence * 100)}")
        send_pi(f"auto_scan {1 if engage.auto_scan else 0}")
    dmap = depth_map(frame)                          # also None when neither the Depth layer nor the depth check is on
    if face and dmap is not None:
        tracker.check_depth(dmap)
    frame = apply_cutout(frame, dmap)
    frame = apply_depth(frame, dmap)
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
            last_seen.seen(now, cx, cy, fw, fh, tracker.confidence)
            if view["box"]:
                draw_reticle(frame, cx, cy, fw, fh, tracker.confidence, engage.min_confidence)
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
    lost = None
    if tracker.armed and not found:
        lost = last_seen.lost(now, (frame.shape[1], frame.shape[0]))
        if lost and view["box"] and lost["ago"] <= LastSeen.GHOST_S:
            draw_ghost(frame, lost)
    info["lost"] = lost
    if view["trail"] and len(trail) > 1:           # older points are thinner and dimmer
        pts = list(trail)
        for i in range(1, len(pts)):
            k = i / len(pts)
            cv2.line(frame, pts[i - 1], pts[i], (int(60 + 195 * k), int(180 + 60 * k), int(40 + 120 * k)), 1 + int(3 * k), cv2.LINE_AA)
    if not found:
        info.update(speed=0.0, settled=False)
        if ctl["aimer"]:
            ctl["aimer"].reset()
    send_pi(f"aim {1 if info['settled'] else 0} {info['speed']:.1f}")
    tracking = ctl["tracking"] and tracker.armed
    _, scan_goal = engage.update(now, tracking, found, info["settled"], info["speed"],
                                 tracker.confidence if found else 0.0)
    if scan_goal and tracking and ctl["aimer"]:      # auto-scan: sweep while searching
        with ctl_lock:
            ctl["angles"] = ctl["aimer"].gimbal.move_to(*scan_goal)
    return cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 90])[1].tobytes(), found


def capture_loop():
    feed = CameraFeed(CAMERA) if CAMERA is not None else FrameFeed()    # a laptop webcam, or the Pi's stream
    feed_ref["feed"] = feed
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
                 "lost": info.get("lost"), "perception": ("TRACKING" if found else (info.get("lost") or {}).get("status", "SEARCHING"))
                 if tracker.armed else None, "cutout_active": cutout_active(), "preset": tracker.preset, "confidence": round(tracker.confidence, 2) if found else 0.0,
                 "conf_parts": tracker.conf_parts if found else {}, "min_confidence": engage.min_confidence,
                 "depth": depth_state["net"].stats() if depth_state["net"] else None, "depth_error": depth_state["error"],
                 "dropped": feed.dropped, "age_ms": round(age_ms), "cfg": dict(cfg),
                 "engage": {"state": engage.state, "armed": engage.armed, "auto_scan": engage.auto_scan,
                            "shots": engage.shots, "max_shots": engage.max_shots},
                 "qnx": qnx_summary()}
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
            last_seen.reset()
            send_pi("clear")                                  # a clicked target is tracked on the laptop, not by the C presets
            return self._send(json.dumps({"ok": ok}).encode())
        if url.path == "/preset" and q.get("name", [""])[0] in PRESETS:
            tracker.use_preset(q["name"][0])
            last_seen.reset()
            send_pi(f"preset {q['name'][0]}")
            return self._send(b"{}")
        if url.path == "/view" and q.get("key", [""])[0] in view:
            view[q["key"][0]] = q.get("on", ["0"])[0] == "1"
            if q["key"][0] in ("mirror", "vflip"):       # the picture just changed orientation: forget the old position
                tracker.last = None
                predictor.reset()
                trail.clear()
                last_seen.reset()
                depth_state["smoother"].reset()
            return self._send(b"{}")
        if url.path == "/arm":
            on = q.get("on", ["0"])[0] == "1"
            engage.arm(on, time.time())
            qnx_sync["arm_cmd"] = time.time()
            send_pi(f"arm {1 if on else 0}")
            return self._send(b"{}")
        if url.path == "/autoscan":
            engage.auto_scan = q.get("on", ["0"])[0] == "1"
            send_pi(f"auto_scan {1 if engage.auto_scan else 0}")
            return self._send(b"{}")
        if url.path == "/cfg" and "cut" in q:
            cfg["cut"] = max(0, min(60, int(float(q["cut"][0]))))
            return self._send(b"{}")
        if url.path == "/cfg" and "min_conf" in q:
            cfg["min_conf"] = max(30, min(100, int(float(q["min_conf"][0]))))
            engage.min_confidence = cfg["min_conf"] / 100
            send_pi(f"min_conf {cfg['min_conf']}")
            return self._send(b"{}")
        if url.path == "/cfg" and "lead_ms" in q:
            cfg["lead_ms"] = max(0, min(800, int(float(q["lead_ms"][0]))))
            return self._send(b"{}")
        if url.path == "/clear":
            tracker.clear()
            last_seen.reset()
            send_pi("clear")
            return self._send(b"{}")
        if url.path == "/mode" and ctl["aimer"] and q.get("name", [""])[0] in ("map", "follow"):
            ctl["aimer"].set_mode(q["name"][0])
            return self._send(b"{}")
        if url.path == "/flip" and ctl["aimer"] and q.get("axis", [""])[0] in ("pan", "tilt"):
            ctl["aimer"].flip(q["axis"][0])
            return self._send(b"{}")
        if url.path == "/center" and ctl["aimer"]:
            with ctl_lock:
                ctl["angles"] = ctl["aimer"].home()
            return self._send(b"{}")
        self._send(b"{}", code=404)


if __name__ == "__main__":
    threading.Thread(target=capture_loop, daemon=True).start()
    print("Turret: http://localhost:8000", flush=True)
    ThreadingHTTPServer(("127.0.0.1", 8000), Handler).serve_forever()
