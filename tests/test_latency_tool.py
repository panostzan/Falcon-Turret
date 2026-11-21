"""Tests the latency tool's analysis on simulated frames. Run: python tests/test_latency_tool.py"""
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "laptop"))
from latency_test import analyze   # noqa: E402


def simulate(latency, fps=15, flashes=10, period=1.5, contrast=120, noise=3.0):
    events = [(10 + i * period, i % 2 == 0) for i in range(flashes)]
    frames, t = [], 10 - 3
    while t < events[-1][0] + period:
        shown = max([e for e in events if e[0] + latency <= t], default=(0, False), key=lambda e: e[0])[1]
        frames.append((t, (40 + contrast if shown else 40) + random.uniform(-noise, noise)))
        t += 1 / fps
    return frames, events


def test():
    random.seed(1)
    for true_ms in (80, 250, 400):
        frames, events = simulate(true_ms / 1000)
        lat, contrast = analyze(frames, events)
        med = sorted(lat)[len(lat) // 2]
        assert len(lat) >= 8 and abs(med - true_ms) <= 70, (true_ms, med, len(lat))   # within one frame period
    frames, events = simulate(0.25, contrast=4)
    try:
        analyze(frames, events)
        raise SystemExit("expected a 'not seeing the flashes' error")
    except ValueError as e:
        assert "aim it at the screen" in str(e)
    print("latency tool tests passed")


if __name__ == "__main__":
    test()
