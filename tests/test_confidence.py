"""Tests target confidence and the rule that a doubtful target never locks or fires. Run: python tests/test_confidence.py"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "laptop"))
import cv2   # noqa: E402
import numpy as np   # noqa: E402
from engage import Engagement   # noqa: E402
from tracker import ColorTracker   # noqa: E402

TEAL_BGR = (170, 170, 20)     # hue ~90


def scene(boxes, size=(480, 640)):
    img = np.full((*size, 3), 90, np.uint8)
    for (x, y, w, h) in boxes:
        cv2.rectangle(img, (x, y), (x + w, y + h), TEAL_BGR, -1)
    return img


def test_tracker():
    t = ColorTracker()
    assert t.preset is None and t.confidence == 0.0
    t.use_preset("teal")
    assert t.preset == "teal"
    confs = []
    for i in range(14):                                      # one solid steady box
        assert t.find(scene([(300, 200, 60, 60)])) is not None
        confs.append(t.confidence)
    assert confs[0] < 0.5 < confs[-1] and confs[-1] > 0.85, confs     # builds up while it keeps being seen
    assert all(b >= a - 1e-9 for a, b in zip(confs, confs[1:])), confs
    assert t.find(scene([])) is None and t.confidence == 0.0 and t.streak == 0

    for _ in range(14):                                      # a rival blob of similar size makes it less sure
        t.find(scene([(300, 200, 60, 60)]))
    alone = t.confidence
    t2 = ColorTracker()
    t2.use_preset("teal")
    for _ in range(14):
        t2.find(scene([(300, 200, 60, 60), (100, 100, 56, 56)]))
    assert t2.confidence < alone - 0.15, (t2.confidence, alone)
    assert t2.conf_parts["unique"] < 0.4

    t3 = ColorTracker()
    t3.use_preset("teal")
    for _ in range(12):
        t3.find(scene([(300, 200, 60, 60)]))
    steady = t3.confidence
    t3.find(scene([(30, 30, 60, 60)]))                       # it jumped across the picture
    assert t3.conf_parts["steady"] < 0.2 and t3.confidence < steady - 0.1

    t3.clear()
    assert t3.preset is None and t3.confidence == 0.0
    print("tracker confidence ok")


def test_engage():
    e = Engagement()
    e.arm(True, 0.0)
    fired, ts = False, 0.0
    for _ in range(80):                                      # settled, slow, but only 60% sure: never locks, never fires
        ts += 0.05
        f, _ = e.update(ts, True, True, True, 0.0, confidence=0.6)
        fired |= f
    assert not fired and e.state == "TRACK" and e.shots == 0, (e.state, e.shots)

    for _ in range(80):                                      # the same with 95% confidence: locks and fires
        ts += 0.05
        f, _ = e.update(ts, True, True, True, 0.0, confidence=0.95)
        fired |= f
    assert fired and e.shots >= 1

    e2 = Engagement()
    e2.arm(True, 0.0)
    ts = 0.0
    for _ in range(30):
        ts += 0.05
        e2.update(ts, True, True, True, 0.0, confidence=0.95)
    assert e2.state in ("LOCK", "COOLDOWN") or e2.shots, e2.state
    e3 = Engagement(dwell_s=5.0)                              # lock, then confidence drops: the lock breaks before any shot
    e3.arm(True, 0.0)
    ts = 0.0
    for _ in range(30):
        ts += 0.05
        e3.update(ts, True, True, True, 0.0, confidence=0.95)
    assert e3.state == "LOCK"
    ts += 0.05
    e3.update(ts, True, True, True, 0.0, confidence=0.4)
    assert e3.state == "TRACK" and e3.shots == 0
    print("engage confidence interlock ok")


if __name__ == "__main__":
    test_tracker()
    test_engage()
    print("confidence tests passed")
