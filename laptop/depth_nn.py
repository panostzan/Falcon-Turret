"""Relative depth from ONE camera with a small neural network (Depth Anything V2 Small, ONNX, CPU).

The result is dense (no holes) and RELATIVE: it says which things are nearer and which are farther, not how many
metres. It is an estimate from what scenes usually look like, not a measurement, so it is shown as an overlay that is
labelled as estimated. It runs on its own thread: submit() never waits, latest() returns the newest finished result,
so a slow network cannot slow the video. If onnxruntime or the model file is missing, available() is False and
error says why; nothing else is affected.

Model file: models/depth_model.onnx  (onnx-community/depth-anything-v2-small, Apache-2.0; see README).
"""
import os
import threading
import time

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_MODEL = os.path.join(HERE, "..", "models", "depth_model.onnx")
SIZE = (308, 224)                    # network input (width, height): multiples of 14. ~7 fps on a laptop CPU.
MEAN = np.array([0.485, 0.456, 0.406], np.float32)
STD = np.array([0.229, 0.224, 0.225], np.float32)


def preprocess(bgr, size=SIZE):
    x = cv2.resize(bgr, size, interpolation=cv2.INTER_AREA)
    x = cv2.cvtColor(x, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    return ((x - MEAN) / STD).transpose(2, 0, 1)[None]


def normalize(depth, previous=None):
    """Scale a raw depth map to 0..1 (1 = nearest) using the scene's own 2nd..98th percentile, eased over time so the
    colours do not flicker. Returns (map, (low, high))."""
    lo, hi = (float(v) for v in np.percentile(depth, [2, 98]))
    hi = max(hi, lo + 1e-3)
    if previous is not None:
        lo, hi = 0.8 * previous[0] + 0.2 * lo, 0.8 * previous[1] + 0.2 * hi
    return np.clip((depth - lo) / (hi - lo), 0, 1).astype(np.float32), (lo, hi)


def overlay(frame_bgr, depth01, alpha=0.55, stale=None):
    """Lay the depth heatmap (warm = near, cool = far) over the picture, with a small legend that says it is estimated."""
    h, w = frame_bgr.shape[:2]
    d = cv2.resize(depth01, (w, h), interpolation=cv2.INTER_CUBIC)
    col = cv2.applyColorMap((255 * np.clip(d, 0, 1)).astype(np.uint8), cv2.COLORMAP_TURBO)
    out = cv2.addWeighted(frame_bgr, 1 - alpha, col, alpha, 0)
    if stale is not None:                      # where the depth is out of date, show the real picture instead
        m = cv2.resize(stale, (w, h), interpolation=cv2.INTER_LINEAR)
        out = cv2.blendLinear(out, frame_bgr, 1.0 - m, m)
    bar = cv2.applyColorMap(np.tile(np.linspace(0, 255, 120, dtype=np.uint8), (8, 1)), cv2.COLORMAP_TURBO)
    x0, y0 = 10, h - 30
    cv2.rectangle(out, (x0 - 4, y0 - 16), (x0 + 124, y0 + 22), (20, 20, 20), -1)
    out[y0:y0 + 8, x0:x0 + 120] = bar
    cv2.putText(out, "far", (x0, y0 + 19), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (235, 235, 235), 1, cv2.LINE_AA)
    cv2.putText(out, "near", (x0 + 94, y0 + 19), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (235, 235, 235), 1, cv2.LINE_AA)
    cv2.putText(out, "depth: estimated, relative", (x0, y0 - 4), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (235, 235, 235), 1, cv2.LINE_AA)
    return out


THUMB = (80, 60)
_WINDOW = cv2.createHanningWindow(THUMB, cv2.CV_32F)     # fades the picture edges out so they do not fool the shift measurement


def thumbnail(bgr):
    """A tiny float grey copy of a picture, used only to measure how far the scene moved between two moments."""
    return cv2.resize(cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY), THUMB, interpolation=cv2.INTER_AREA).astype(np.float32)


def smoothstep(x):
    x = min(1.0, max(0.0, x))
    return x * x * (3 - 2 * x)


class DepthSmoother:
    """Makes a slow depth stream (about 7 results a second) look smooth on a faster video, with three display tricks.
    None of them makes the depth more accurate or newer; they only change how it is shown.
      1. Cross-fade: when a new result arrives, glide from what is on screen to the new map over about one result
         interval, instead of stepping. (Costs about one interval of extra lag on the depth layer.)
      2. Temporal averaging: each new map is blended with the previous one, which removes frame-to-frame flicker.
      3. Motion compensation: the stored map is shifted by the scene's measured movement since it was computed
         (phase correlation on tiny grey copies), so it stays stuck to the scene while the camera pans.
    """

    KEEP = 0.45          # share of the previous map kept when averaging a new one in
    MAX_SHIFT = 12.0     # thumbnail pixels; a bigger apparent move is not trusted (a cut, or a person walking by)

    def __init__(self):
        self.reset()

    def reset(self):
        self.target = self.shown_from = None      # depth maps (the new one, and what was on screen when it arrived)
        self.src = None                           # thumbnail of the picture `target` was computed from
        self.t_new = None
        self.interval = 0.15                      # running estimate of the time between results
        self.fade = 0.15                          # how long the current fade takes (shortened when things move)
        self.motion = 0.0                         # 0 still .. 1 fast-changing picture, measured between results
        self.done = None

    def update(self, depth, done, thumb, now):
        """Feed a result. Ignored if it is not new."""
        if self.done == done:
            return
        if self.target is None:
            self.target = self.shown_from = depth.copy()
        else:
            # How much did the picture change since the previous result? When it changed a lot (things are moving), old
            # depth is stale: skip the averaging and fade fast, so the overlay follows the movement instead of ghosting.
            if self.src is not None and thumb is not None:
                self.motion = smoothstep((float((np.abs(thumb - self.src) > 20).mean()) - 0.01) / 0.08)
            self.shown_from = self.current(now, thumb)            # start the fade from exactly what is on screen now
            keep = self.KEEP * (1.0 - self.motion)
            self.target = keep * self.target + (1 - keep) * depth
            if self.done is not None:
                self.interval = min(0.4, max(0.08, 0.7 * self.interval + 0.3 * (done - self.done)))
            self.fade = self.interval * (1.0 - 0.8 * self.motion)
        self.src, self.done, self.t_new = thumb, done, now

    def _shift(self, thumb):
        """How far (dx, dy in depth-map pixels) the scene moved between the picture behind the target and `thumb`."""
        if self.src is None or thumb is None:
            return 0.0, 0.0
        (dx, dy), response = cv2.phaseCorrelate(self.src, thumb, _WINDOW)
        if response < 0.15 or abs(dx) > self.MAX_SHIFT or abs(dy) > self.MAX_SHIFT:
            return 0.0, 0.0
        if abs(dx) < 0.3 and abs(dy) < 0.3:                     # measurement noise, not movement
            return 0.0, 0.0
        return dx, dy

    def stale_mask(self, thumb):
        """Where the live picture no longer matches the one the depth was computed from (something moved there since),
        as a 0..1 map at thumbnail size, or None. The overlay fades out there, so a fast-moving arm shows the real video
        instead of an out-of-date depth. Camera movement is compensated first, so panning does not mark everything."""
        if self.src is None or thumb is None:
            return None
        src = self.src
        dx, dy = self._shift(thumb)
        if dx or dy:
            src = cv2.warpAffine(src, np.float32([[1, 0, dx], [0, 1, dy]]), THUMB, borderMode=cv2.BORDER_REPLICATE)
        diff = cv2.GaussianBlur(np.abs(thumb - src), (5, 5), 0)
        return np.clip((diff - 10.0) / 25.0, 0.0, 1.0).astype(np.float32)

    def current(self, now, thumb=None):
        """The depth map to show right now (the fade, then the motion compensation)."""
        if self.target is None:
            return None
        k = smoothstep((now - self.t_new) / self.fade)
        d = self.shown_from * (1 - k) + self.target * k if k < 1.0 else self.target
        dx, dy = self._shift(thumb)
        if dx or dy:
            h, w = d.shape[:2]
            m = np.float32([[1, 0, dx * w / THUMB[0]], [0, 1, dy * h / THUMB[1]]])
            d = cv2.warpAffine(d, m, (w, h), borderMode=cv2.BORDER_REPLICATE)
        return d


class NeuralDepth:
    def __init__(self, model_path=DEFAULT_MODEL, size=SIZE, threads=6):
        self.size, self.error, self.ms, self.fps = size, None, 0.0, 0.0
        self._session = None
        self._range = None
        self._latest = None                       # (normalized map, time finished)
        self._thumb = None                        # tiny grey copy of the picture that result came from
        self._pending = None
        self._cv = threading.Condition()
        self._stop = False
        try:
            import onnxruntime as ort
            options = ort.SessionOptions()
            options.intra_op_num_threads = threads      # leave some CPU for the video and the server
            self._session = ort.InferenceSession(model_path, options, providers=["CPUExecutionProvider"])
        except ImportError:
            self.error = "onnxruntime is not installed (pip install onnxruntime)"
        except Exception as e:                           # missing or unreadable model file
            self.error = f"cannot load the depth model {os.path.abspath(model_path)}: {e}"
        if self._session:
            threading.Thread(target=self._run, daemon=True).start()

    @property
    def available(self):
        return self._session is not None

    def run_once(self, bgr):
        """Blocking: raw depth for one picture (higher = nearer), at the network's output size."""
        out = self._session.run(None, {"pixel_values": preprocess(bgr, self.size)})[0]
        return out[0].astype(np.float32)

    def submit(self, bgr):
        """Hand over the newest picture; returns at once. An unprocessed older picture is replaced, never queued."""
        if not self.available:
            return
        with self._cv:
            self._pending = bgr.copy()
            self._cv.notify()

    def latest(self):
        """(normalized depth 0..1, seconds since it finished) or None before the first result."""
        with self._cv:
            if self._latest is None:
                return None
            depth, done = self._latest
        return depth, time.time() - done

    def latest_full(self):
        """(depth, time finished, thumbnail of the picture it came from) or None. For the smoother."""
        with self._cv:
            return None if self._latest is None else (self._latest[0], self._latest[1], self._thumb)

    def _run(self):
        last = time.time()
        while True:
            with self._cv:
                self._cv.wait_for(lambda: self._pending is not None or self._stop)
                if self._stop:
                    return
                frame, self._pending = self._pending, None
            t = time.perf_counter()
            thumb = thumbnail(frame)
            try:
                depth, self._range = normalize(self.run_once(frame), self._range)
            except Exception as e:
                self.error = f"depth inference failed: {e}"
                continue
            self.ms = (time.perf_counter() - t) * 1000
            now = time.time()
            self.fps = 0.8 * self.fps + 0.2 / max(1e-3, now - last) if self.fps else 1 / max(1e-3, now - last)
            last = now
            with self._cv:
                self._latest = (depth, now)
                self._thumb = thumb

    def stats(self):
        return {"available": self.available, "error": self.error, "ms": round(self.ms), "fps": round(self.fps, 1)}

    def close(self):
        with self._cv:
            self._stop = True
            self._cv.notify_all()
