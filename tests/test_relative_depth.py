"""Tests the relative-depth maths on synthetic stereo pairs. Run: python tests/test_relative_depth.py"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))
import cv2   # noqa: E402
import numpy as np   # noqa: E402
import relative_depth as rd   # noqa: E402


def scene(shift_near=20, shift_far=6, seed=1):
    """Left/right pictures of a textured background (far: small shift) with a textured box in front (near: big shift).
    A point at column x in the RIGHT picture appears at x + shift in the LEFT picture, as for a real left camera."""
    rng = np.random.default_rng(seed)
    w, h = rd.SIZE
    far = cv2.GaussianBlur(rng.integers(0, 255, (h, w), dtype=np.uint8), (3, 3), 0)
    near = cv2.GaussianBlur(rng.integers(0, 255, (h, w), dtype=np.uint8), (3, 3), 0)
    r0, r1, c0, c1 = 120, 260, 160, 340                                         # the near box in the RIGHT picture
    right = far.copy()
    right[r0:r1, c0:c1] = near[r0:r1, c0:c1]
    left = np.roll(far, shift_far, axis=1)
    left[r0:r1, c0 + shift_near:c1 + shift_near] = np.roll(near, shift_near, axis=1)[r0:r1, c0 + shift_near:c1 + shift_near]
    return left, right


def test():
    left, right = scene()
    m = rd.make_matcher(96, 7)
    d = rd.disparity(m, left, right)
    assert (d > 0).mean() > 0.5, f"a well-textured pair should be mostly valid, got {(d > 0).mean():.0%}"
    near, far = np.median(d[150:230, 220:320]), np.median(d[20:90, 40:140])
    assert near > far + 8, f"the near box must read as clearly nearer than the background: near {near:.1f}, far {far:.1f}"

    ok, a, b = rd.pick_order(m, left, right)               # correct order is recognised, swapped order is too
    assert ok and a > b
    ok, a, b = rd.pick_order(m, right, left)
    assert not ok and b > a

    flat = np.full(rd.SIZE[::-1], 128, np.uint8)           # no texture: refuses to invent depth
    assert (rd.disparity(m, flat, flat) > 0).mean() < 0.05

    col = rd.colorize(d, 96)                               # invalid is black; near is warmer (more red) than far
    assert col.shape == d.shape + (3,) and (col[d <= 0] == 0).all()
    assert int(col[200, 250][2]) > int(col[50, 80][2]), "near should be redder than far"
    assert rd.make_matcher(50).getNumDisparities() % 16 == 0
    print("relative depth tests passed")


if __name__ == "__main__":
    test()
