"""Turret server.  Run: python laptop/app.py   then open http://localhost:8000

Relays the Pi's JPEG frames to the browser. Click a target in the video to lock its color (a box appears).
When tracking is switched on from the dashboard the server also follows the clicked target and nudges the gimbal (ESP32 on COM7) to center it. Tracking starts OFF.
Binds to localhost only.
"""
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from aim import Aimer              # noqa: E402
from gimbal import Gimbal          # noqa: E402
from receiver import jpeg_frames   # noqa: E402
from tracker import PRESETS, ColorTracker   # noqa: E402

# "COM7" for USB serial, or e.g. "udp:192.168.137.50" for the Wi-Fi firmware. Override: set GIMBAL=udp:...
GIMBAL_PORT = os.environ.get("GIMBAL", "COM7")
PAGE = open(os.path.join(HERE, "dashboard.html"), encoding="utf-8").read().encode()
BOOT = time.time()                  # lets the page detect a server restart and reconnect its video

new_frame = threading.Condition()   # notified whenever `latest` changes
latest = {"jpg": None, "seq": 0, "state": {"connected": False, "fps": 0, "kb": 0, "boot": BOOT}}

tracker = ColorTracker()
raw = {"w": 640, "h": 480}          # frame size, for mapping clicks
# This turret's servos: tilt turns the opposite way from the picture's vertical axis. The dashboard's
# Flip buttons change these live.
DEFAULT_SIGNS = {"pan": 1, "tilt": -1}
view = {"mirror": True, "vflip": False}   # flip the picture so it matches the real world (set from the dashboard)
ctl = {"tracking": False, "aimer": None, "error": None, "angles": None}
ctl_lock = threading.Lock()


def set_tracking(on):
    """Switch tracking on/off; opens the gimbal on first use. Returns an error string or None."""
    with ctl_lock:
        if not on:
            ctl["tracking"] = False
            return None
        try:
            if ctl["aimer"] is None:
                ctl["aimer"] = Aimer(Gimbal(GIMBAL_PORT))
                ctl["aimer"].sign.update(DEFAULT_SIGNS)
            ctl["angles"] = ctl["aimer"].gimbal.status()
            ctl["tracking"], ctl["error"] = True, None
        except Exception as e:          # port busy / ESP unplugged / bad reply
            ctl["tracking"], ctl["error"] = False, f"gimbal: {e}"
        return ctl["error"]


def process(jpg):
    """Apply the view flips and, if a target is armed, find it, aim, and draw. Returns (JPEG, found flag)."""
    if not (view["mirror"] or view["vflip"] or tracker.armed):
        return jpg, False                # nothing to change: the Pi's JPEG goes straight to the browser
    frame = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
    if view["mirror"] or view["vflip"]:
        frame = cv2.flip(frame, -1 if view["mirror"] and view["vflip"] else 1 if view["mirror"] else 0)
    found = False
    if tracker.armed:
        face = tracker.find(frame)
        h, w = frame.shape[:2]
        raw.update(w=w, h=h)
        cv2.drawMarker(frame, (w // 2, h // 2), (255, 255, 255), cv2.MARKER_CROSS, 20, 1)
        if face:
            found = True
            cx, cy, fw, fh = face
            cv2.rectangle(frame, (int(cx - fw / 2), int(cy - fh / 2)), (int(cx + fw / 2), int(cy + fh / 2)), (0, 255, 0), 2)
            try:
                with ctl_lock:
                    if ctl["tracking"]:
                        ctl["angles"] = ctl["aimer"].update((cx - w / 2) / (w / 2), (cy - h / 2) / (h / 2))
            except Exception as e:
                with ctl_lock:
                    ctl["tracking"], ctl["error"] = False, f"gimbal: {e}"
    return cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 90])[1].tobytes(), found


def capture_loop():
    arrivals, count = [], 0
    for ts, jpg in jpeg_frames():
        jpg, found = process(jpg)
        arrivals = [a for a in arrivals + [ts] if ts - a < 3]
        fps = (len(arrivals) - 1) / (arrivals[-1] - arrivals[0]) if len(arrivals) > 1 else 0
        count += 1
        aimer = ctl["aimer"]
        state = {"connected": True, "fps": round(fps, 1), "frames": count, "kb": round(len(jpg) / 1024),
                 "boot": BOOT, "tracking": ctl["tracking"], "target": found, "armed": tracker.armed, "error": ctl["error"],
                 "angles": ctl["angles"], "sign": aimer.sign if aimer else None,
                 "mode": aimer.mode if aimer else "map", "view": dict(view)}
        with new_frame:
            latest.update(jpg=jpg, seq=count, state=state)
            new_frame.notify_all()


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
        if self.path == "/":
            self._send(PAGE, "text/html; charset=utf-8")
        elif self.path == "/state":
            self._send(json.dumps(latest["state"]).encode())
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
            tracker.last = None          # the picture just changed orientation: forget the old position
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
