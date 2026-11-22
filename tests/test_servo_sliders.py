"""Tests the slider tool's logic with a fake gimbal. Run: python tests/test_servo_sliders.py"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["TURRET_TILT_CALIBRATION"] = "none.json"
os.environ["TURRET_LIFT_CALIBRATION"] = "none.json"
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
sys.path.insert(0, os.path.join(HERE, "..", "laptop"))
import servo_sliders as ss   # noqa: E402


class FakeGimbal:
    def __init__(self):
        self.sent, self.calls = [], []

    def move_to(self, pan=None, tilt=None, lift=None):
        self.sent.append({k: v for k, v in (("pan", pan), ("tilt", tilt), ("lift", lift)) if v is not None})

    def center(self):
        self.calls.append("center")

    def release(self):
        self.calls.append("release")

    def set_oscillator(self, khz):
        self.calls.append(("osc", khz))


def test():
    lines = []
    g = FakeGimbal()
    c = ss.SliderControl(g, pan=90, tilt=22, lift=90, max_step=6, log=lines.append)
    assert c.update() is False and g.sent == [], "nothing happens until a slider moves"

    c.set_target("pan", 150)                                 # a sudden big drag is walked there, never jumped
    steps = []
    while c.update():
        steps.append(c.sent["pan"])
    assert steps == [96, 102, 108, 114, 120, 126, 132, 138, 144, 150], steps
    assert all(list(m) == ["pan"] for m in g.sent), "only pan was sent while only pan moved"

    c.set_target("tilt", 500)                                # clamped to what the servo takes
    c.set_target("pan", -40)
    c.set_target("lift", 300)
    assert c.target == {"pan": 0, "tilt": 180, "lift": 150}, "the lift stops at 150"
    g.sent.clear()
    c.update()
    assert g.sent == [{"pan": 144}, {"tilt": 28}, {"lift": 96}], g.sent      # all three move together, each limited to the step

    c.max_step = 20                                          # the speed limit is adjustable
    g.sent.clear()
    c.update()
    assert g.sent == [{"pan": 124}, {"tilt": 48}, {"lift": 116}]

    c.nudge("pan", 10)                                       # keys nudge the target
    assert c.target["pan"] == 10
    c.centre()
    assert g.calls[-1] == "center" and c.target == {"pan": 90, "tilt": 22, "lift": 90} and c.sent == c.target
    c.release()
    c.set_oscillator(26000)
    assert g.calls[-2:] == ["release", ("osc", 26000)]

    c2 = ss.SliderControl(FakeGimbal(), pan=90, tilt=100, lift=90)    # tilt marks: TOP and BOTTOM give the calibration
    c2.sent["tilt"] = 40
    assert c2.mark("top") is None
    c2.sent["tilt"] = 120
    cal = c2.mark("bottom")
    assert cal["window"] == [42, 118] and cal["sign"] == 1, cal
    c2.marks = {}
    c2.sent["tilt"] = 100
    c2.mark("top")
    c2.sent["tilt"] = 20
    assert c2.mark("bottom")["sign"] == -1, "bottom at the LOWER angle flips the direction"

    c3 = ss.SliderControl(FakeGimbal(), pan=90, tilt=22, lift=90)     # lift marks: LOW and HIGH give the platform's range
    c3.sent["lift"] = 120
    assert c3.mark("low") is None
    c3.sent["lift"] = 30
    assert c3.mark("high") == {"low": 30, "high": 120}, "given in either order, stored low-to-high"
    c3.marks = {"low": 50}
    c3.sent["lift"] = 52
    assert c3.mark("high") is None, "two marks almost on top of each other are not a range"

    assert any("commanded pan" in l for l in lines)
    print("servo sliders logic ok")


if __name__ == "__main__":
    test()
