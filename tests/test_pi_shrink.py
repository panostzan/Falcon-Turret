"""Checks the Pi's picture-shrinking code against a trusted numpy version. Run: python tests/test_pi_shrink.py"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "pi"))
import numpy as np   # noqa: E402
from shrink import drop_4th_columns, drop_4th_rows, shrink_planes   # noqa: E402


def keep(n):
    return [i for i in range(n) if i % 4 != 3]


def test():
    rng = np.random.default_rng(3)
    W, H = 640, 480
    y = rng.integers(0, 256, (H, W), dtype=np.uint8)
    u = rng.integers(0, 256, (H, W // 2), dtype=np.uint8)
    v = rng.integers(0, 256, (H, W // 2), dtype=np.uint8)
    ys, us, vs = shrink_planes(y.tobytes(), u.tobytes(), v.tobytes(), W, H)
    ref_y = y[np.ix_(keep(H), keep(W))]
    ref_u = u[np.ix_(keep(H), keep(W // 2))]
    assert len(ys) == 480 * 360 and len(us) == len(vs) == 240 * 360, (len(ys), len(us), len(vs))
    assert (np.frombuffer(ys, np.uint8).reshape(360, 480) == ref_y).all(), "Y plane differs from the reference"
    assert (np.frombuffer(us, np.uint8).reshape(360, 240) == ref_u).all(), "U plane differs from the reference"
    assert (np.frombuffer(vs, np.uint8).reshape(360, 240) == v[np.ix_(keep(H), keep(W // 2))]).all()

    flat = bytes(range(16))                               # a tiny case by hand: 4 wide, 4 tall
    assert bytes(drop_4th_columns(flat)) == bytes([0, 1, 2, 4, 5, 6, 8, 9, 10, 12, 13, 14])
    assert drop_4th_rows(bytes(range(12)), 3, 4) == bytes(range(9)), "rows 0-2 kept, row 3 dropped"

    # a smooth picture stays smooth (a straight selection must not shear it): a horizontal ramp keeps its order
    ramp = np.tile((np.arange(W) // 3).astype(np.uint8), (H, 1))
    out = np.frombuffer(shrink_planes(ramp.tobytes(), ramp[:, ::2].tobytes(), ramp[:, ::2].tobytes(), W, H)[0], np.uint8).reshape(360, 480)
    assert (np.diff(out.astype(int), axis=1) >= 0).all() and (out[:, 0] == 0).all()
    print("picture shrinking matches the reference")


if __name__ == "__main__":
    test()
