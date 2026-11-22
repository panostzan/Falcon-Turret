"""Tests the slider tool's logic with a fake gimbal. Run: python tests/test_servo_sliders.py"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["TURRET_TILT_CALIBRATION"] = "none.json"
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
sys.path.insert(0, os.path.join(HERE, "..", "laptop"))
import servo_sliders as ss   # noqa: E402


class FakeGimbal:
    def __init__(self):
        self.sent = []

    def move_to(self, pan=None, tilt=None):
        self.sent.append((pan, tilt))


def test():
    lines = []
    g = FakeGimbal()
    c = ss.SliderControl(g, pan=90, tilt=22, max_step=6, log=lines.append)
    assert c.update() is False and g.sent == [], "nothing happens until a slider moves"

    c.set_target("pan", 150)                                 # a sudden big drag is walked there, never jumped
    steps = []
    while c.update():
        steps.append(c.sent["pan"])
    assert steps == [96, 102, 108, 114, 120, 126, 132, 138, 144, 150], steps
    assert all(t is None for _, t in g.sent), "tilt untouched while only pan moved"

    c.set_target("tilt", 500)                                # clamped to what the servo takes
    c.set_target("pan", -40)
    assert c.target == {"pan": 0, "tilt": 180}
    g.sent.clear()
    c.update()
    assert g.sent == [(144, None), (None, 28)], g.sent       # both servos move together, each limited to the step

    c.max_step = 20                                          # the speed limit is adjustable
    g.sent.clear()
    c.update()
    assert g.sent[0] == (124, None) and g.sent[1] == (None, 48)

    c.nudge("pan", 10)                                       # keys nudge the target
    assert c.target["pan"] == 10
    c.home()
    assert c.target == {"pan": 90, "tilt": 22}

    c2 = ss.SliderControl(FakeGimbal(), pan=90, tilt=100)    # marks: TOP and BOTTOM give the calibration
    c2.sent["tilt"] = 40
    assert c2.mark("top") is None
    c2.sent["tilt"] = 120
    cal = c2.mark("bottom")
    assert cal["window"] == [42, 118] and cal["sign"] == 1, cal
    c2.sent["tilt"] = 100
    c2.marks = {}
    c2.mark("top")
    c2.sent["tilt"] = 20
    assert c2.mark("bottom")["sign"] == -1, "bottom at the LOWER angle flips the direction"

    assert any("commanded pan" in l for l in lines)
    print("servo sliders logic ok")


if __name__ == "__main__":
    test()
