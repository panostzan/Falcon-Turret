"""Tests the depth-aware confidence with synthetic depth maps. Run: python tests/test_depth_confidence.py"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "laptop"))
import cv2   # noqa: E402
import numpy as np   # noqa: E402
from tracker import ColorTracker   # noqa: E402


def scene(x=300, y=200, w=80, h=80):
    img = np.full((480, 640, 3), 90, np.uint8)
    cv2.rectangle(img, (x, y), (x + w, y + h), (170, 170, 20), -1)
    return img


def depth_flat(value, shape=(224, 308)):
    return np.full(shape, value, np.float32)


def settle(t, depth, frames=14, **box):
    for _ in range(frames):
        assert t.find(scene(**box)) is not None
        t.check_depth(depth)


def test():
    t = ColorTracker()
    t.use_preset("teal")
    settle(t, None)                                          # no depth map: nothing changes
    base = t.confidence
    assert base > 0.85 and "depth" not in t.conf_parts

    t = ColorTracker()
    t.use_preset("teal")
    settle(t, depth_flat(0.7))                               # the target is all at one depth: no penalty
    assert t.conf_parts["depth"] > 0.95 and t.confidence > 0.85, (t.conf_parts, t.confidence)

    t2 = ColorTracker()
    t2.use_preset("teal")
    split = depth_flat(0.2)
    split[:, 164:] = 0.9                                     # the box (x 300-380) spans a far object and a near one, half and half
    settle(t2, split)
    assert t2.conf_parts["depth_consistent"] < 0.7 and t2.confidence < 0.8 < t.confidence, (t2.conf_parts, t2.confidence)

    # a jump to a different depth after following the target for a while (e.g. the wrong teal object takes over)
    settle(t, depth_flat(0.7), frames=6)
    good = t.confidence
    t.find(scene())
    t.check_depth(depth_flat(0.15))
    assert t.conf_parts["depth_steady"] < 0.1 and t.confidence < good * 0.7, (t.conf_parts, t.confidence, good)
    ref_after = t.depth_ref
    assert ref_after > 0.6, "a doubtful match must not rewrite what we think the target's depth is"

    # slow, legitimate depth change (target approaching): tolerated
    t3 = ColorTracker()
    t3.use_preset("teal")
    for k in range(40):
        t3.find(scene())
        t3.check_depth(depth_flat(0.4 + 0.01 * k))
    assert t3.conf_parts["depth_steady"] > 0.9 and t3.confidence > 0.85, (t3.conf_parts, t3.confidence)

    t3.use_preset("blue")
    assert t3.depth_ref is None                              # choosing a new target forgets the old one's depth
    t3.clear()
    assert t3.blob is None
    t3.check_depth(depth_flat(0.5))                          # nothing tracked: harmless
    print("depth-aware confidence tests passed")


if __name__ == "__main__":
    test()
