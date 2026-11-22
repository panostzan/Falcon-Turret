"""Tests the aiming logic with a simulated gimbal + target. Run: python tests/test_aim.py"""
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "laptop"))
from aim import H_FOV, V_FOV, Aimer, pixel_error_to_degrees   # noqa: E402
from gimbal import CENTER, PAN_RANGE, TILT_RANGE               # noqa: E402

T_LO, T_HI = TILT_RANGE
T_MID = (T_LO + T_HI) / 2


class SimGimbal:
    """Pan-only world with a fixed target at `target` degrees. The camera sees the target `delay` frames
    late (like our real pipeline) and reports its offset using the real field of view.
    `direction=-1` models a servo that turns the wrong way relative to the image."""

    def __init__(self, target, direction=1, delay=0):
        self.pan, self.tilt, self.target, self.direction = 90, CENTER[1], target, direction
        self.history = [90] * (delay + 1)
        self.sent = []

    def move_to(self, pan=None, tilt=None):   # like the real Gimbal: integer degrees, clamped
        if pan is not None:
            self.pan = int(round(max(PAN_RANGE[0], min(PAN_RANGE[1], pan))))
        if tilt is not None:
            self.tilt = int(round(max(TILT_RANGE[0], min(TILT_RANGE[1], tilt))))
        self.history = self.history[1:] + [self.pan]
        self.sent.append((self.pan, self.tilt))
        return self.pan, self.tilt

    def error(self):
        gap = max(-80, min(80, self.direction * (self.target - self.history[0])))   # a camera can't see past +-90
        return max(-1, min(1, math.tan(math.radians(gap)) / math.tan(math.radians(H_FOV / 2))))


def run(g, steps=80, flip=False):
    a = Aimer(g, mode="follow")
    if flip:
        a.flip("pan")
    path = []
    for _ in range(steps):
        a.update(g.error(), 0.0)
        path.append(g.pan)
    return a, path


def in_range(g):
    return all(PAN_RANGE[0] <= p <= PAN_RANGE[1] and TILT_RANGE[0] <= t <= TILT_RANGE[1] for p, t in g.sent)


def test_map():
    # same image position -> same angle, and it holds still (no counting up)
    g = SimGimbal(90)
    a = Aimer(g)                                       # map mode is the default
    for _ in range(40):
        a.update(0.5, 0.0)
    held = g.sent[-10:]
    assert len(set(held)) == 1, f"must hold one angle for a still target, got {set(held)}"
    expect = 90 + 0.5 * 90                              # linear: half way to the right edge = 135
    assert abs(g.pan - expect) <= 1.5, f"x=+0.5 should map to ~{expect:.1f}, got {g.pan}"
    n_before = len(g.sent)
    for _ in range(20):
        a.update(0.5, 0.0)
    assert len(set(g.sent[n_before:])) == 1, "angle drifted while the target stood still"

    a.update(0.0, 0.0)                                  # target back at the center -> straight ahead
    for _ in range(20):
        a.update(0.0, 0.0)
    assert abs(g.pan - 90) <= 2.5 and abs(g.tilt - T_MID) <= 2.5, (g.pan, g.tilt)   # within the hold-still band

    for ex in (-1.0, -0.5, 0.25, 1.0):                  # symmetric, bounded, and monotonic mapping
        g = SimGimbal(90)
        a = Aimer(g)
        for _ in range(30):
            a.update(ex, 0.0)
        assert abs(g.pan - (90 + ex * 90)) <= 1.5, (ex, g.pan)

    g = SimGimbal(90)                                   # tilt maps around the middle of its range and stays in it
    a = Aimer(g)
    for _ in range(30):
        a.update(0.0, 1.0)
    assert abs(g.tilt - T_HI) <= 1.5 and in_range(g)

    g = SimGimbal(90)                                   # Flip reverses the direction
    a = Aimer(g)
    a.flip("pan")
    for _ in range(30):
        a.update(0.5, 0.0)
    assert g.pan < 90, g.pan

    g = SimGimbal(90)                                   # tiny wobble in the target does not move the servo
    a = Aimer(g)
    for _ in range(20):
        a.update(0.30, 0.0)
    settled = g.pan
    for e in (0.299, 0.302, 0.298, 0.301):
        a.update(e, 0.0)
    assert abs(g.pan - settled) <= 1, (g.pan, settled)
    for ex, want in ((-1.0, 0), (1.0, 180), (0.0, 90)):   # the whole picture covers the whole pan range
        g = SimGimbal(90)
        a = Aimer(g)
        for _ in range(40):
            a.update(ex, 0.0)
        assert abs(g.pan - want) <= 1.5, f"x={ex}: pan {g.pan}, want {want}"
    for ey, want in ((-1.0, T_LO), (1.0, T_HI), (0.0, T_MID)):   # ...and the whole tilt range
        g = SimGimbal(90)
        a = Aimer(g)
        for _ in range(40):
            a.update(0.0, ey)
        assert abs(g.tilt - want) <= 1.5, f"y={ey}: tilt {g.tilt}, want {want}"
    print("map mode tests passed")


def test_no_hunting():
    """A nearly still target seen through pixel noise must not keep the servo busy, and a real move must be quick."""
    import random
    rnd = random.Random(2)
    g = SimGimbal(90)
    a = Aimer(g)
    last, moves = (g.pan, g.tilt), 0
    for _ in range(150):                                   # ~10 s at 15 fps, 4 px of box-center noise
        a.update(rnd.gauss(0, 4) / 320, rnd.gauss(0, 4) / 240)
        if (g.pan, g.tilt) != last:
            moves, last = moves + 1, (g.pan, g.tilt)
    assert moves <= 15, f"servo hunted: {moves} moves in 10 s on a still target"
    final = 90 + (480 - 320) / 320 * 90                    # now the target jumps 160 px to the right
    frames = 0
    while abs(g.pan - final) > 3 and frames < 30:
        a.update((480 + rnd.gauss(0, 4) - 320) / 320, 0.0)
        frames += 1
    assert frames <= 2, f"a real move should be followed within 2 frames, took {frames}"


def test():
    test_no_hunting()
    test_map()
    # the field of view for a 60 degree diagonal, 4:3 picture
    assert abs(H_FOV - 49.6) < 0.2 and abs(V_FOV - 38.2) < 0.2, (H_FOV, V_FOV)
    assert abs(pixel_error_to_degrees(1.0, H_FOV) - H_FOV / 2) < 1e-6, "frame edge = half the field of view"
    assert pixel_error_to_degrees(0.0, H_FOV) == 0

    for delay in (0, 3):                              # no delay, and 3 frames late like the real pipeline
        for target in (130, 60, 105):
            g = SimGimbal(target, delay=delay)
            _, path = run(g)
            assert abs(g.pan - target) <= 3, f"delay {delay}: should settle on {target}, got {g.pan}"
            overshoot = max(path) - target if target > 90 else target - min(path)
            assert overshoot <= 10, f"delay {delay}, target {target}: overshoot {overshoot} degrees"
            assert in_range(g)

    g = SimGimbal(250)                                # beyond reach: sits at the end stop
    run(g)
    assert g.pan == PAN_RANGE[1] and in_range(g)
    g = SimGimbal(-100)
    run(g)
    assert g.pan == PAN_RANGE[0] and in_range(g)

    g = SimGimbal(130, direction=-1)                  # flipped servo runs to a stop, stays in range; Flip fixes it
    run(g)
    assert in_range(g)
    g = SimGimbal(130, direction=-1, delay=3)
    run(g, flip=True)
    assert abs(g.pan - 130) <= 3

    g = SimGimbal(90)                                 # tilt can't leave its range either
    a = Aimer(g, mode="follow")
    for _ in range(40):
        a.update(0.0, -1.0)
    assert g.tilt == TILT_RANGE[0] and in_range(g)
    for _ in range(40):
        a.update(0.0, 1.0)
    assert g.tilt == TILT_RANGE[1] and in_range(g)

    g = SimGimbal(90)
    a = Aimer(g, mode="follow")
    a.update(0.02, -0.03)                             # under 1 degree: ignored
    assert g.sent[-1] == (90, CENTER[1])
    before = g.sent[-1]
    a.update(1.0, 1.0)                                # huge error: step capped
    assert abs(g.sent[-1][0] - before[0]) <= a.max_step + 1 and abs(g.sent[-1][1] - before[1]) <= a.max_step + 1
    print("all aim tests passed")


def test_tilt_window():
    """A tilt window (0-45, the bracket's travel): bottom of the picture = 45, top = 0, middle = 22.5; never outside it."""
    g = SimGimbal(target=90)
    a = Aimer(g, tilt_window=(0, 45))
    a.sign["tilt"] = 1
    assert a.tilt_center == 22.5 and a.span["tilt"] == 45
    for ey, expect in ((1.0, 45), (-1.0, 0), (0.0, 22.5), (0.5, 33.75)):
        a.reset()
        a._goal = None
        a.update(0.0, ey)
        assert abs(g.tilt - expect) <= 1.0, (ey, g.tilt, expect)
    for ey in (-5.0, 5.0, 0.3, -0.7):                      # even a wild reading stays inside the window
        a.reset()
        a.update(0.0, ey)
        assert 0 <= g.tilt <= 45, g.tilt
    f = Aimer(g, mode="follow", tilt_window=(0, 45))
    for _ in range(300):
        f.update(0.0, 1.0)                                 # follow mode pushes down for a long time: it stops at 45
    assert g.tilt == 45, g.tilt
    for _ in range(300):
        f.update(0.0, -1.0)
    assert g.tilt == 0, g.tilt
    a.home()
    assert abs(g.tilt - 22.5) <= 1 and g.pan == 90, (g.pan, g.tilt)
    full = Aimer(SimGimbal(target=90))                     # without a window: the whole range the firmware allows
    assert full.tilt_center == T_MID and full.tilt_clamp == TILT_RANGE
    print("tilt window ok")


if __name__ == "__main__":
    test()
    test_tilt_window()
