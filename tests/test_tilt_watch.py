"""Tests the tilt watch tool's logic with a fake gimbal (no window, no hardware). Run: python tests/test_tilt_watch.py"""
import os
os.environ["TURRET_TILT_CALIBRATION"] = "none.json"   # never read the operator's real calibration in a test
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
sys.path.insert(0, os.path.join(HERE, "..", "laptop"))
import tilt_watch as tw   # noqa: E402


class FakeGimbal:
    def __init__(self):
        self.tilt, self.sent = 22, []

    def move_to(self, pan=None, tilt=None):
        self.tilt = tilt
        self.sent.append(tilt)


def test():
    lines = []
    g = FakeGimbal()
    c = tw.TiltControl(g, start=22, log=lines.append)
    assert c.jog(1) == 23 and c.jog(-5) == 18 and g.sent == [23, 18]
    assert c.jog(-500) == 0 and c.jog(500) == 180, "never outside what the firmware accepts"
    c.set(30)
    c.set_limits(40, 20)                                   # given the wrong way round: still sane
    assert (c.lo, c.hi) == (20, 40)
    c.set_limits(-50, 999)
    assert (c.lo, c.hi) == (0, 180)

    c.set_limits(10, 20)
    c.set(15)
    c.sweeping = True
    seen = [c.sweep_step() for _ in range(40)]
    assert min(seen) == 10 and max(seen) == 20, (min(seen), max(seen))
    assert all(abs(b - a) == 1 for a, b in zip(seen, seen[1:])), "one degree at a time"
    c.jog(2)
    assert not c.sweeping, "a key press stops the sweep"

    # limits with no room to sweep (both clamp to 180): it must not jump, and must not keep "sweeping" in place
    g2 = FakeGimbal()
    c2 = tw.TiltControl(g2, start=50)
    c2.set(50)
    c2.set_limits(300, 400)
    assert (c2.lo, c2.hi) == (180, 180)
    c2.sweeping = True
    sent_before = list(g2.sent)
    c2.sweep_step()
    assert g2.sent == sent_before and not c2.sweeping, "no room to sweep: nothing is sent and the sweep stops"
    # limits that exclude the current angle: walk back one degree at a time, never jump
    c3 = tw.TiltControl(FakeGimbal(), start=50)
    c3.set_limits(10, 20)
    c3.sweeping = True
    seen = [c3.sweep_step() for _ in range(70)]
    assert seen[0] == 49 and all(abs(b - a) <= 1 for a, b in zip([50] + seen, seen)), "never more than one degree per step"
    assert min(seen) == 10 and max(seen) <= 50

    c.set(12)
    c.mark("low")
    assert c.marks == {"low": 12} and any("MARK low = 12" in l for l in lines)
    assert any("commanded tilt" in l for l in lines)
    print("tilt watch logic ok")


if __name__ == "__main__":
    test()
