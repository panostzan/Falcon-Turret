"""Rolling telemetry: per-frame timing and aim data kept in ring buffers, with summary statistics."""
import threading
from collections import deque

KEYS = ("interval_ms", "age_ms", "proc_ms", "pan", "tilt", "speed")


def percentile(values, p):
    """p-th percentile (0-100) of a list by linear interpolation; 0.0 for an empty list."""
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * p / 100.0
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


class Telemetry:
    def __init__(self, size=300):
        self._lock = threading.Lock()
        self._series = {k: deque(maxlen=size) for k in KEYS}
        self._last_ts = None

    def add(self, ts, **values):
        """Record one frame. interval_ms is computed from the previous frame's time."""
        with self._lock:
            interval = None if self._last_ts is None else (ts - self._last_ts) * 1000
            self._last_ts = ts
            values["interval_ms"] = interval
            for key in KEYS:
                v = values.get(key)
                self._series[key].append(None if v is None else float(v))

    def stats(self, key):
        with self._lock:
            vals = [v for v in self._series[key] if v is not None]
        if not vals:
            return {"n": 0, "mean": 0.0, "p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0}
        return {"n": len(vals), "mean": sum(vals) / len(vals), "p50": percentile(vals, 50),
                "p95": percentile(vals, 95), "p99": percentile(vals, 99), "max": max(vals)}

    def snapshot(self, points=120):
        """Series downsampled to at most `points` values each (None = no data), plus summary statistics."""
        with self._lock:
            series = {k: list(v) for k, v in self._series.items()}
        out = {}
        for key, vals in series.items():
            step = max(1, len(vals) // points)
            out[key] = [None if v is None else round(v, 1) for v in vals[::step]]
        return {"series": out, "stats": {k: {a: round(b, 1) for a, b in self.stats(k).items()} for k in KEYS}}
