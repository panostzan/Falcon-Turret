"""Tests the telemetry ring buffers and statistics. Run: python tests/test_telemetry.py"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "laptop"))
from telemetry import Telemetry, percentile   # noqa: E402


def test():
    assert percentile([], 50) == 0.0
    assert percentile([5], 99) == 5
    assert percentile(list(range(101)), 50) == 50 and percentile(list(range(101)), 95) == 95
    assert abs(percentile([1, 2, 3, 4], 50) - 2.5) < 1e-9

    t = Telemetry(size=100)
    for i in range(250):                                   # more than the buffer holds
        t.add(i * 0.0667, age_ms=40 + (i % 10), proc_ms=3.0, pan=90 + i % 5, tilt=135, speed=None)
    s = t.stats("age_ms")
    assert s["n"] == 100 and 40 <= s["p50"] <= 49 and s["max"] == 49
    assert abs(t.stats("interval_ms")["p50"] - 66.7) < 0.5, "frame interval comes from the timestamps"
    assert t.stats("speed")["n"] == 0, "missing values are skipped in the statistics"
    snap = t.snapshot(points=20)
    assert all(len(v) <= 20 for v in snap["series"].values())
    assert snap["series"]["speed"][0] is None
    assert set(snap["stats"]) == {"interval_ms", "age_ms", "proc_ms", "pan", "tilt", "speed"}
    print("telemetry tests passed")


if __name__ == "__main__":
    test()
