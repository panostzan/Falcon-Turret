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


def overlay(frame_bgr, depth01, alpha=0.55):
    """Lay the depth heatmap (warm = near, cool = far) over the picture, with a small legend that says it is estimated."""
    h, w = frame_bgr.shape[:2]
    d = cv2.resize(depth01, (w, h), interpolation=cv2.INTER_CUBIC)
    col = cv2.applyColorMap((255 * np.clip(d, 0, 1)).astype(np.uint8), cv2.COLORMAP_TURBO)
    out = cv2.addWeighted(frame_bgr, 1 - alpha, col, alpha, 0)
    bar = cv2.applyColorMap(np.tile(np.linspace(0, 255, 120, dtype=np.uint8), (8, 1)), cv2.COLORMAP_TURBO)
    x0, y0 = 10, h - 30
    cv2.rectangle(out, (x0 - 4, y0 - 16), (x0 + 124, y0 + 22), (20, 20, 20), -1)
    out[y0:y0 + 8, x0:x0 + 120] = bar
    cv2.putText(out, "far", (x0, y0 + 19), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (235, 235, 235), 1, cv2.LINE_AA)
    cv2.putText(out, "near", (x0 + 94, y0 + 19), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (235, 235, 235), 1, cv2.LINE_AA)
    cv2.putText(out, "depth: estimated, relative", (x0, y0 - 4), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (235, 235, 235), 1, cv2.LINE_AA)
    return out


class NeuralDepth:
    def __init__(self, model_path=DEFAULT_MODEL, size=SIZE, threads=6):
        self.size, self.error, self.ms, self.fps = size, None, 0.0, 0.0
        self._session = None
        self._range = None
        self._latest = None                       # (normalized map, time finished)
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

    def _run(self):
        last = time.time()
        while True:
            with self._cv:
                self._cv.wait_for(lambda: self._pending is not None or self._stop)
                if self._stop:
                    return
                frame, self._pending = self._pending, None
            t = time.perf_counter()
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

    def stats(self):
        return {"available": self.available, "error": self.error, "ms": round(self.ms), "fps": round(self.fps, 1)}

    def close(self):
        with self._cv:
            self._stop = True
            self._cv.notify_all()
