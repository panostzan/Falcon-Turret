"""Simulates moving targets seen through a delayed, noisy camera and compares aiming with and without lead.
Run: python tests/test_predict.py"""
import math
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "laptop"))
from predict import LeadPredictor   # noqa: E402

W, H = 640, 480
FPS = 15.0
DELAY = 0.25          # seconds between the scene and the aim code seeing it


def simulate(path, lead_s, seconds=6.0, noise=1.5, seed=3, raw=False, **kw):
    """path(t) -> (x, y) true position. The aim code sees path(t - DELAY) plus noise at each frame.
    Returns RMS error (pixels) between the aim point and the TRUE position at the moment of aiming.
    raw=True aims straight at the measurement (what the turret did before prediction existed)."""
    rnd = random.Random(seed)
    p = LeadPredictor(**kw)
    sq, n, t = 0.0, 0, 0.0
    while t < seconds:
        mx, my = path(t - DELAY)
        mx, my = mx + rnd.gauss(0, noise), my + rnd.gauss(0, noise)
        aim = (mx, my) if raw else p.update(t, mx, my, lead_s, (W, H))
        if t > 1.0:                                        # skip the start-up
            tx, ty = path(t)
            sq += (aim[0] - tx) ** 2 + (aim[1] - ty) ** 2
            n += 1
        t += 1.0 / FPS + rnd.uniform(-0.01, 0.01)          # slightly uneven frame timing
    return math.sqrt(sq / n)


def still(t):
    return 320.0, 240.0


def constant(t):                                          # 90 px/s to the right (stays inside the picture)
    return 60.0 + 90.0 * max(t, 0), 240.0


def wave(t):                                              # swings left-right: 2 s period, +-150 px
    return 320 + 150 * math.sin(2 * math.pi * t / 2.0), 240 + 60 * math.sin(2 * math.pi * t / 3.0)


def stop_and_go(t):                                       # runs, stops dead, runs back
    if t < 2.0:
        return 100 + 150 * t, 240.0
    if t < 4.0:
        return 400.0, 240.0
    return 400 - 150 * (t - 4.0), 240.0


def test():
    lead = DELAY
    results = {}
    for name, path in (("still", still), ("constant speed", constant), ("swinging", wave), ("stop and go", stop_and_go)):
        raw = simulate(path, 0.0, raw=True)               # no prediction: aims where the frame says
        led = simulate(path, lead)
        results[name] = (raw, led)
        print(f"{name:15s} error without lead {raw:6.1f} px  with lead {led:6.1f} px")

    raw, led = results["constant speed"]
    assert raw > 15 and led < raw * 0.3, f"constant speed: {raw:.1f} -> {led:.1f}"
    raw, led = results["swinging"]
    assert led < raw * 0.8, f"swinging: {raw:.1f} -> {led:.1f}"
    raw, led = results["stop and go"]
    assert led < raw * 0.5, f"stop and go: {raw:.1f} -> {led:.1f}"
    raw, led = results["still"]
    assert led <= raw + 0.5, f"a still target must not jitter more: {raw:.1f} -> {led:.1f}"

    # more lead than the real delay overshoots, and the cap stops it flinging the aim across the picture
    p = LeadPredictor()
    for i in range(60):
        aim = p.update(i / FPS, 50.0 + 400 * i / FPS, 240, 3.0, (W, H))
    assert 0 <= aim[0] <= W and aim[0] - (50 + 400 * 59 / FPS) <= 0.3 * W + 1, aim

    # a gap with no measurement forgets the old velocity (no flying off after a lost target)
    p = LeadPredictor()
    for i in range(30):
        p.update(i / FPS, 100.0 + 200 * i / FPS, 240, 0.25, (W, H))
    assert p.velocity[0] > 100
    aim = p.update(5.0, 500.0, 240, 0.25, (W, H))
    assert aim == (500.0, 240.0) and p.velocity == (0.0, 0.0)
    print("motion prediction tests passed")


if __name__ == "__main__":
    test()
