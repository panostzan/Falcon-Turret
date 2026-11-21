"""Bounded frame pairing and a latest-only processing worker; no device ownership."""
from collections import deque
from dataclasses import dataclass
import math
import threading
import time


@dataclass(frozen=True)
class Frame:
    image: object
    timestamp_s: float
    camera_id: str
    clock_domain: str


class FramePairer:
    """Monotonic ordered streams in one clock domain. Not exposure synchronization.

    push('left'/'right', Frame) returns (left,right) or None. Frames are copied
    on admission; a frame is never reused. Old unmatched frames are dropped.
    Earliest feasible pair wins; no wait for hypothetical closer future frames.
    """
    def __init__(self, left_id, right_id, max_skew_s=0.015, capacity=3, max_wait_s=0.2):
        if not left_id or not right_id or left_id == right_id:
            raise ValueError("Distinct camera IDs required")
        if not isinstance(capacity, int) or capacity < 1:
            raise ValueError("capacity must be positive")
        if any(not math.isfinite(v) or v <= 0 for v in (max_skew_s, max_wait_s)):
            raise ValueError("Timing limits must be finite and positive")
        self.ids = {"left": left_id, "right": right_id}
        self.max_skew_s, self.capacity, self.max_wait_s = max_skew_s, capacity, max_wait_s
        self._queues = {"left": deque(), "right": deque()}
        self._last = {"left": -1, "right": -1}
        self._domain = None
        self._lock = threading.Lock()
        self.dropped = 0

    def push(self, side, frame):
        if side not in self.ids or frame.camera_id != self.ids[side]:
            raise ValueError("Camera identity/side mismatch")
        if not math.isfinite(frame.timestamp_s) or frame.timestamp_s < 0 or not frame.clock_domain:
            raise ValueError("A valid timestamp and clock domain are required")
        with self._lock:
            if self._domain is not None and frame.clock_domain != self._domain:
                raise ValueError("Cannot pair different clock domains; recreate pairer after clock reset")
            if frame.timestamp_s <= self._last[side]:
                raise ValueError("Duplicate/out-of-order timestamp; recreate pairer after clock reset")
            self._domain = frame.clock_domain
            self._last[side] = frame.timestamp_s
            now = time.monotonic()
            for q in self._queues.values():
                while q and now-q[0][1] > self.max_wait_s:
                    q.popleft()
                    self.dropped += 1
            q = self._queues[side]
            if len(q) == self.capacity:
                q.popleft()
                self.dropped += 1
            q.append((Frame(frame.image.copy(), frame.timestamp_s, frame.camera_id, frame.clock_domain), now))
            l, r = self._queues["left"], self._queues["right"]
            while l and r:
                delta = l[0][0].timestamp_s-r[0][0].timestamp_s
                if abs(delta) <= self.max_skew_s:
                    return l.popleft()[0], r.popleft()[0]
                (l if delta < 0 else r).popleft()
                self.dropped += 1
            return None


class StereoWorker:
    """One in-flight + one pending pair; bounded queue, newest submission wins.

    submit copies inputs and does not wait for stereo computation. latest() never
    waits for computation and expires results based on LOCAL admission time,
    not the remote capture clock. No claim of end-to-end capture freshness.
    """
    def __init__(self, processor, max_age_s=0.2):
        if not math.isfinite(max_age_s) or max_age_s <= 0:
            raise ValueError("max_age_s must be finite and positive")
        self.processor, self.max_age_s = processor, max_age_s
        self._cv = threading.Condition()
        self._pending = self._latest = None
        self._closed = False
        self.error = None
        self.dropped = 0
        self._thread = threading.Thread(target=self._run, name="stereo-depth", daemon=True)
        self._thread.start()

    def submit(self, left, right, *, timestamps_s, clock_domain):
        # Live path requires capture timing; synchronous process supports offline images.
        if timestamps_s is None or not clock_domain:
            raise ValueError("Live worker requires timestamps and a common capture clock domain")
        if len(timestamps_s) != 2 or not all(math.isfinite(t) and t >= 0 for t in timestamps_s):
            raise ValueError("Invalid timestamps")
        if abs(timestamps_s[0]-timestamps_s[1]) > self.processor.config.max_skew_s:
            raise ValueError("Excessive frame skew")
        from .core import gray
        gray(left, self.processor.calibration.image_size)
        gray(right, self.processor.calibration.image_size)
        with self._cv:
            if self._closed:
                raise RuntimeError("Worker is closed")
            if self._pending is not None:
                self.dropped += 1
            self._pending = (left.copy(), right.copy(), tuple(timestamps_s), clock_domain, time.monotonic())
            self._cv.notify()

    def latest(self):
        with self._cv:
            if self._latest is None or time.monotonic()-self._latest[1] > self.max_age_s:
                return None
            return self._latest[0]

    def _run(self):
        while True:
            with self._cv:
                self._cv.wait_for(lambda: self._closed or self._pending is not None)
                if self._closed:
                    return
                l, r, ts, domain, admitted = self._pending
                self._pending = None
            if time.monotonic()-admitted > self.max_age_s:
                with self._cv:
                    self.dropped += 1
                continue
            try:
                result = self.processor.process(l, r, timestamps_s=ts, clock_domain=domain)
                with self._cv:
                    if self._closed:
                        return
                    self._latest = (result, admitted)
                    self.error = None
            except Exception as exc:
                with self._cv:
                    self.error = str(exc)
                    self._latest = None

    def close(self, timeout=2.0):
        with self._cv:
            self._closed = True
            self._pending = self._latest = None
            self._cv.notify_all()
        self._thread.join(timeout)
        if self._thread.is_alive():
            raise TimeoutError("Stereo computation did not finish before shutdown timeout")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
