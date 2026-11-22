"""Tests the servo sweep with a fake gimbal (no hardware). Run: python tests/test_servo_sweep.py"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
sys.path.insert(0, os.path.join(HERE, "..", "laptop"))
import servo_sweep as sw   # noqa: E402


class FakeGimbal:
    def __init__(self):
        self.pan, self.tilt, self.log, self.closed = 90, 22, [], False

    def move_to(self, pan=None, tilt=None):
        if pan is not None:
            self.pan = pan
        if tilt is not None:
            self.tilt = tilt
        self.log.append((self.pan, self.tilt))
        return self.pan, self.tilt

    def status(self):
        return self.pan, self.tilt

    def close(self):
        self.closed = True


def test():
    assert sw.legs(140, 180, 2)[0] == 140 and sw.legs(140, 180, 2)[-1] == 180
    assert sw.legs(0, 180, 7)[-1] == 180 and max(b - a for a, b in zip(sw.legs(0, 180, 7), sw.legs(0, 180, 7)[1:])) <= 7

    g = FakeGimbal()
    sent = sw.sweep_axis(g, "pan", 0, 180, 3, 2, 0, sleep=lambda s: None, say=lambda *a: None)
    assert min(sent) == 0 and max(sent) == 180
    assert sum(1 for a, b in zip(sent, sent[1:]) if b == 180 and a != 180) == 3, "reaches 180 three times"
    assert sum(1 for a, b in zip([None] + sent, sent) if b == 0 and a != 0) >= 3, "returns to 0 each time"
    assert all(abs(b - a) <= 2 for a, b in zip([90] + sent, sent)), "no jump bigger than one step, even from the start position"
    assert g.tilt == 22, "the pan sweep never touches tilt"
    assert g.pan == 90, "ends in the middle"

    g = FakeGimbal()                                        # tilt stays inside its window
    sent = sw.sweep_axis(g, "tilt", 0, 45, 3, 2, 0, sleep=lambda s: None, say=lambda *a: None)
    assert min(sent) == 0 and max(sent) == 45
    assert all(0 <= a <= 45 for a in sent), "never outside the window"
    assert all(abs(b - a) <= 2 for a, b in zip([22] + sent, sent))
    assert g.pan == 90 and g.tilt == 22

    g = FakeGimbal()                                        # the whole program, with a wider tilt range and one round trip
    import io
    import contextlib
    with contextlib.redirect_stdout(io.StringIO()) as out:
        sw.time.sleep = lambda s: None                      # no real waiting in the test
        sw.main(["--cycles", "1", "--axis", "tilt", "--tilt", "0,60"], gimbal=g)
    assert min(a for _, a in g.log) == 0 and max(a for _, a in g.log) == 60 and g.closed
    assert "done" in out.getvalue()

    for bad in (["--cycles", "0"], ["--step", "50"], ["--delay", "0"], ["--tilt", "90,180"], ["--tilt", "30,10"], ["--tilt", "x"]):
        try:
            sw.main(bad, gimbal=FakeGimbal())
            raise AssertionError("bad option accepted: " + str(bad))
        except SystemExit:
            pass
    print("servo sweep tests passed")


if __name__ == "__main__":
    test()
