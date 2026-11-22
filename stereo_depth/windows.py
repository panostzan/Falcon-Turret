"""Optional Windows USB webcam test source. Never imported by the depth processor.

DirectShow indexes are NOT stable camera identities. Check physical left/right
in preview after reconnecting. Timestamps measure host receipt, NOT exposure.
"""
import argparse
import json
import os
from pathlib import Path
import threading
import time

import cv2
import numpy as np

from .core import Calibration, Config, StereoDepth
from .stream import Frame, FramePairer
from .view import panel


class WindowsStereo:
    def __init__(self, left_index, right_index, size=(640, 480), fps=30,
                 left_id="left-c270", right_id="right-c270", max_skew_s=.025,
                 backend="dshow"):
        if os.name != "nt":
            raise RuntimeError("WindowsStereo is a Windows test adapter; QNX supplies its own frames")
        if left_index == right_index or min(left_index, right_index) < 0:
            raise ValueError("Choose two distinct nonnegative camera indices")
        if backend not in ("dshow", "msmf"):
            raise ValueError("backend must be dshow or msmf")
        if len(size) != 2 or any(not isinstance(v, int) or v <= 0 for v in size) or not np.isfinite(fps) or fps <= 0:
            raise ValueError("Invalid capture dimensions/fps")
        self.size, self.fps = tuple(size), fps
        self.backend = cv2.CAP_DSHOW if backend == "dshow" else cv2.CAP_MSMF
        self._pairer = FramePairer(left_id, right_id, max_skew_s=max_skew_s)
        self._stop = threading.Event()
        self._cv = threading.Condition()
        self._latest = None
        self._errors = []
        self.received = {"left": 0, "right": 0}
        self.dropped_pairs = 0
        self._threads = [threading.Thread(target=self._capture, args=(side, index, identity),
                          name="stereo-usb-"+side, daemon=True)
                         for side, index, identity in (("left", left_index, left_id), ("right", right_index, right_id))]
        for thread in self._threads:
            thread.start()

    def _capture(self, side, index, identity):
        camera = None
        try:
            camera = cv2.VideoCapture(index, self.backend)
            if not camera.isOpened():
                raise RuntimeError(f"Cannot open {side} camera index {index}; check indices, privacy permissions, and other camera apps")
            camera.set(cv2.CAP_PROP_FRAME_WIDTH, self.size[0])
            camera.set(cv2.CAP_PROP_FRAME_HEIGHT, self.size[1])
            camera.set(cv2.CAP_PROP_FPS, self.fps)
            while not self._stop.is_set():
                ok, image = camera.read()
                stamp = time.monotonic()
                if not ok or image is None:
                    raise RuntimeError(f"{side} camera stopped delivering frames")
                if image.shape[:2] != (self.size[1], self.size[0]):
                    raise RuntimeError(f"{side} camera returned {image.shape[1]}x{image.shape[0]}, requested {self.size}; calibrate at the actual mode")
                pair = self._pairer.push(side, Frame(image, stamp, identity, "windows-host-receipt-monotonic"))
                with self._cv:
                    self.received[side] += 1
                    if pair is not None:
                        if self._latest is not None:
                            self.dropped_pairs += 1
                        self._latest = (pair, time.monotonic())
                        self._cv.notify_all()
        except Exception as exc:
            with self._cv:
                self._errors.append(str(exc))
                self._stop.set()
                self._cv.notify_all()
        finally:
            if camera is not None:
                camera.release()

    def read(self, timeout=5.0, max_age_s=.2):
        """Consume newest paired frames or raise; timeout reports clock/driver issues."""
        deadline = time.monotonic()+timeout
        with self._cv:
            while True:
                if self._errors:
                    raise RuntimeError("; ".join(self._errors))
                if self._stop.is_set():
                    raise RuntimeError("Camera source is closed")
                if self._latest is not None:
                    pair, admitted = self._latest
                    self._latest = None
                    if time.monotonic()-admitted <= max_age_s:
                        return pair
                remaining = deadline-time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"No fresh pair; frames={self.received}, unmatched drops={self._pairer.dropped}. Check both feeds and --max-skew-ms.")
                self._cv.wait(remaining)

    def close(self):
        self._stop.set()
        with self._cv:
            self._latest = None
            self._cv.notify_all()
        for thread in self._threads:
            thread.join(2)
        if any(t.is_alive() for t in self._threads):
            raise TimeoutError("Windows camera read is blocked in its driver; close the test process to release it")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def alignment_view(left, right, alpha=0.5, guides=True):
    """Raw views and overlays for inspection, not geometric calibration."""
    left = cv2.resize(left, (480, 360), interpolation=cv2.INTER_AREA)
    right = cv2.resize(right, (480, 360), interpolation=cv2.INTER_AREA)
    blend = cv2.addWeighted(left, alpha, right, 1-alpha, 0)
    anaglyph = np.empty_like(left)
    anaglyph[:, :, 2] = cv2.cvtColor(left, cv2.COLOR_BGR2GRAY)
    anaglyph[:, :, :2] = cv2.cvtColor(right, cv2.COLOR_BGR2GRAY)[:, :, None]
    tiles = [left, right, blend, anaglyph]
    labels = ["LEFT - raw", "RIGHT - raw", "BLEND - adjust slider", "RED left / CYAN right"]
    for tile, label in zip(tiles, labels):
        if guides:
            for y in range(60, 360, 60):
                cv2.line(tile, (0, y), (479, y), (80, 210, 80), 1)
            cv2.line(tile, (240, 0), (240, 359), (80, 210, 80), 1)
        cv2.rectangle(tile, (0, 0), (479, 30), (20, 20, 20), -1)
        cv2.putText(tile, label, (10, 21), cv2.FONT_HERSHEY_SIMPLEX, .55, (255, 255, 255), 1, cv2.LINE_AA)
    image = np.vstack((np.hstack(tiles[:2]), np.hstack(tiles[2:])))
    footer = np.full((48, 960, 3), 20, np.uint8)
    cv2.putText(footer, "UNCALIBRATED | G: guides | Q/ESC: close | horizontal separation is normal",
                (10, 29), cv2.FONT_HERSHEY_SIMPLEX, .52, (255, 255, 255), 1, cv2.LINE_AA)
    return np.vstack((image, footer))


class PairRecorder:
    """Save paired PNGs + calibration manifest without touching existing files."""
    def __init__(self, directory, left_id, right_id):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        if any(self.directory.iterdir()):
            raise ValueError("Use an empty/new recording directory")
        self.data = {"left_id": left_id, "right_id": right_id, "pairs": []}

    def add(self, left, right):
        if (left.camera_id, right.camera_id) != (self.data["left_id"], self.data["right_id"]):
            raise ValueError("Recording camera identities changed")
        if not left.clock_domain or left.clock_domain != right.clock_domain:
            raise ValueError("Recording frames must share a clock domain")
        index = len(self.data["pairs"])
        names = [f"left_{index:03d}.png", f"right_{index:03d}.png"]
        for name, frame in zip(names, (left, right)):
            if not cv2.imwrite(str(self.directory/name), frame.image):
                raise OSError("Could not save " + name)
        self.data["pairs"].append({"left": names[0], "right": names[1],
            "timestamps_s": [left.timestamp_s, right.timestamp_s], "clock_domain": left.clock_domain})
        temp = self.directory/"manifest.tmp"
        temp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        temp.replace(self.directory/"manifest.json")


def main():
    p = argparse.ArgumentParser(description="Windows-only stereo test; no Pi needed. Camera receipt times are not exposure timestamps.")
    p.add_argument("mode", choices=("preview", "record", "depth", "check"))
    p.add_argument("--left", type=int, required=True, help="OpenCV index; verify physical left in preview")
    p.add_argument("--right", type=int, required=True)
    p.add_argument("--backend", choices=("dshow", "msmf"), default="dshow")
    p.add_argument("--size", type=int, nargs=2, default=(640, 480))
    p.add_argument("--fps", type=float, default=30)
    p.add_argument("--max-skew-ms", type=float, default=25, help="Host receipt skew limit only; not exposure synchronization")
    p.add_argument("--calibration")
    p.add_argument("--output", type=Path)
    p.add_argument("--board", nargs=2, type=int, default=(9, 6))
    p.add_argument("--algorithm", choices=("bm", "sgbm"), default="bm")
    p.add_argument("--frames", type=int, default=100, help="Number of pairs for headless check")
    args = p.parse_args()
    if args.frames < 1:
        p.error("--frames must be positive")
    cal = Calibration.load(args.calibration) if args.calibration else None
    if args.mode == "depth" and cal is None:
        p.error("depth requires --calibration; no guessed baseline/focal length")
    if args.mode == "record" and args.output is None:
        p.error("record requires --output to a new directory")
    if cal and tuple(args.size) != cal.image_size:
        p.error("--size must match the calibration input resolution")
    left_id, right_id = (cal.left_id, cal.right_id) if cal else ("left-c270", "right-c270")
    processor = StereoDepth(cal, Config(algorithm=args.algorithm, max_skew_s=args.max_skew_ms/1000)) if args.mode == "depth" else None
    recorder = PairRecorder(args.output, left_id, right_id) if args.mode == "record" else None
    title = "Stereo alignment - side by side + overlay" if args.mode == "preview" else "Stereo USB test - Q/ESC quit; SPACE save checkerboard pair"
    guides = True
    result = None
    def clicked(event, x, y, flags, param):
        if event == cv2.EVENT_LBUTTONDOWN and result is not None:
            w = result.depth_m.shape[1]
            if 0 <= y < result.depth_m.shape[0] and 0 <= x < 2*w:
                print(f"Rectified ({x % w}, {y}): Z={result.distance_at(x % w, y, 2)} m", flush=True)
    if args.mode != "check":
        cv2.namedWindow(title)
        cv2.setMouseCallback(title, clicked)
        if args.mode == "preview":
            cv2.createTrackbar("Left blend %", title, 50, 100, lambda value: None)
    skew, count, started = [], 0, time.monotonic()
    try:
        with WindowsStereo(args.left, args.right, tuple(args.size), args.fps, left_id, right_id,
                           args.max_skew_ms/1000, args.backend) as source:
            while True:
                l, r = source.read(timeout=10 if count == 0 else 3)
                count += 1
                delta = abs(l.timestamp_s-r.timestamp_s)
                skew.append(delta*1000)
                if len(skew) > 1000:
                    del skew[0]
                if args.mode == "check":
                    if count >= args.frames:
                        break
                    continue
                if args.mode == "preview":
                    display = alignment_view(l.image, r.image, cv2.getTrackbarPos("Left blend %", title)/100, guides)
                elif processor:
                    result = processor.process(l.image, r.image, timestamps_s=(l.timestamp_s, r.timestamp_s), clock_domain=l.clock_domain)
                    display = panel(result)
                else:
                    display = np.hstack((l.image, r.image))
                    cv2.putText(display, "LEFT", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)
                    cv2.putText(display, "RIGHT", (args.size[0]+10, 30), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)
                    cv2.putText(display, f"Receipt skew {delta*1000:.1f}ms | saved {len(recorder.data['pairs']) if recorder else 0}",
                                (10, 60), cv2.FONT_HERSHEY_SIMPLEX, .6, (0, 255, 0), 1)
                cv2.imshow(title, display)
                if count == 1:
                    print("Viewer running: both camera frames received and displayed.", flush=True)
                key = cv2.waitKey(1) & 0xff
                if key in (27, ord('q')):
                    break
                if cv2.getWindowProperty(title, cv2.WND_PROP_VISIBLE) < 1:
                    break
                if key == ord('g'):
                    guides = not guides
                if key == 32 and recorder:
                    found = [cv2.findChessboardCorners(cv2.cvtColor(f.image, cv2.COLOR_BGR2GRAY), tuple(args.board))[0] for f in (l, r)]
                    if all(found):
                        recorder.add(l, r)
                        print(f"Saved pair {len(recorder.data['pairs'])}; keep the board still for each capture", flush=True)
                    else:
                        print("Not saved: checkerboard not detected in BOTH views", flush=True)
    finally:
        if args.mode != "check":
            cv2.destroyAllWindows()
    print(json.dumps({"pairs": count, "elapsed_s": time.monotonic()-started,
        "receipt_skew_p95_ms": float(np.percentile(skew, 95)) if skew else None,
        "note": "Host receipt timestamps only. Validate static distances first; motion can corrupt depth."}, indent=2))


if __name__ == "__main__":
    main()
