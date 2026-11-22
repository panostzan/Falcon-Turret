"""Tests the tilt watch tool's logic with a fake gimbal (no window, no hardware). Run: python tests/test_tilt_watch.py"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
sys.path.insert(0, os.path.join(HERE, "..", "laptop"))
import tilt_watch as tw   # noqa: E402


class FakeGimbal:
    def __init__(self):
        self.tilt, self.sent = 135, []

    def move_to(self, pan=None, tilt=None):
        self.tilt = tilt
        self.sent.append(tilt)


def test():
    lines = []
    g = FakeGimbal()
    c = tw.TiltControl(g, start=135, log=lines.append)
    assert c.jog(1) == 136 and c.jog(-5) == 131 and g.sent == [136, 131]
    assert c.jog(-500) == 90 and c.jog(500) == 180, "never outside what the firmware accepts"
    c.set(160)
    c.set_limits(170, 150)                                 # given the wrong way round: still sane
    assert (c.lo, c.hi) == (150, 170)
    c.set_limits(0, 999)
    assert (c.lo, c.hi) == (90, 180)

    c.set_limits(140, 150)
    c.set(145)
    c.sweeping = True
    seen = [c.sweep_step() for _ in range(40)]
    assert min(seen) == 140 and max(seen) == 150, (min(seen), max(seen))
    assert all(abs(b - a) == 1 for a, b in zip(seen, seen[1:])), "one degree at a time"
    c.jog(2)
    assert not c.sweeping, "a key press stops the sweep"

    # the bug from the log: low 0 and high 90 become 90-90. It must not jump, and must not keep "sweeping" at 90.
    g2 = FakeGimbal()
    c2 = tw.TiltControl(g2, start=156)
    c2.set(156)
    c2.set_limits(0, 90)
    assert (c2.lo, c2.hi) == (90, 90)
    c2.sweeping = True
    sent_before = list(g2.sent)
    c2.sweep_step()
    assert g2.sent == sent_before and not c2.sweeping, "no room to sweep: nothing is sent and the sweep stops"
    # limits that exclude the current angle: walk back one degree at a time, never jump
    c3 = tw.TiltControl(FakeGimbal(), start=156)
    c3.set_limits(100, 110)
    c3.sweeping = True
    seen = [c3.sweep_step() for _ in range(70)]
    assert seen[0] == 155 and all(abs(b - a) <= 1 for a, b in zip([156] + seen, seen)), "never more than one degree per step"
    assert min(seen) == 100 and max(seen[:60]) <= 156

    c.set(152)
    c.mark("low")
    assert c.marks == {"low": 152} and any("MARK low = 152" in l for l in lines)
    assert any("commanded tilt" in l for l in lines)
    print("tilt watch logic ok")


if __name__ == "__main__":
    test()
