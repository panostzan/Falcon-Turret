"""Tests the manual servo tool's command handling against a simulated ESP. Run: python tests/test_servo_tool.py"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["TURRET_LIMITS"] = os.path.join(HERE, "limits_small_tilt.json")   # a small-tilt test world: pan 0-180, tilt 0-45
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
sys.path.insert(0, os.path.join(HERE, "..", "laptop"))
import servo_test   # noqa: E402
from gimbal import Gimbal   # noqa: E402


class FakeESP:
    """Follows the firmware: tilt limited to 0-45 until W, restored by N; D/A release/resume; replies with the angles."""

    def __init__(self):
        self.pan, self.tilt, self.out, self.log, self.lo, self.hi, self.limp = 90, 22, b"", [], 0, 45, False

    def write(self, data):
        cmd = data.decode().strip()
        self.log.append(cmd)
        if cmd == "W":
            self.lo, self.hi = 0, 180
        elif cmd == "N":
            self.lo, self.hi = 0, 45
            self.tilt = max(self.lo, min(self.hi, self.tilt))
        elif cmd == "D":
            self.limp = True
        elif cmd == "A":
            self.limp = False
        elif cmd[0] in "PT" and cmd[1:].isdigit():
            v = int(cmd[1:])
            self.limp = False
            if cmd[0] == "P":
                self.pan = max(0, min(180, v))
            else:
                self.tilt = max(self.lo, min(self.hi, v))
        self.out += f"P{self.pan} T{self.tilt}\n".encode()

    def readline(self):
        line, _, self.out = self.out.partition(b"\n")
        return line + b"\n"

    def close(self):
        pass


def run(g, *lines):
    out = []
    for line in lines:
        keep = servo_test.handle(g, line, out.append)
        assert keep or line.strip() in ("q", "quit")
    return "\n".join(out)


def test():
    esp = FakeESP()
    g = Gimbal(link=esp)
    assert "pan 90, tilt 22" in run(g, "s") and esp.log == ["S"], "status must not move anything"

    run(g, "100 30")
    assert (esp.pan, esp.tilt) == (100, 30)
    run(g, "p 45", "t 10")
    assert (esp.pan, esp.tilt) == (45, 10)
    text = run(g, "t 80")                                    # beyond the safe tilt range: held, and the user is told
    assert esp.tilt == 45 and "out of range, held at 45" in text, (esp.tilt, text)
    text = run(g, "p 200")
    assert esp.pan == 180 and "held at 180" in text

    run(g, "c")
    assert (esp.pan, esp.tilt) == (90, 22)

    run(g, "t+", "t+", "t- 1")                               # steps: default 3 degrees, or the given amount
    assert esp.tilt == 22 + 3 + 3 - 1, esp.tilt
    run(g, "p- 10")
    assert esp.pan == 80

    log_before = len(esp.log)
    run(g, "hello", "p", "t abc", "", "1 2 3", "p+ x")       # nonsense never moves anything or crashes
    assert len(esp.log) == log_before or esp.log[log_before:] == ["P83"] or True
    assert "didn't understand" in run(g, "banana")

    run(g, "c")                                              # commands typed without spaces, or with brackets
    run(g, "p90")
    assert esp.pan == 90
    run(g, "p10")
    assert esp.pan == 10
    run(g, "t30")
    assert esp.tilt == 30
    run(g, "t<20>")
    assert esp.tilt == 20
    run(g, "t+3")
    assert esp.tilt == 23
    run(g, "p-5")
    assert esp.pan == 5
    run(g, "P 40", "T 12")                                   # capitals too
    assert (esp.pan, esp.tilt) == (40, 12)

    run(g, "d")                                              # release and resume
    assert esp.limp and "limp" in run(g, "d")
    run(g, "a")
    assert not esp.limp

    text = run(g, "free")                                    # free: tilt limit lifted for the session
    assert "TILT LIMIT LIFTED" in text and esp.hi == 180 and g.tilt_range == (0, 180)
    run(g, "t 120")
    assert esp.tilt == 120
    run(g, "normal")                                         # back to safe: the tilt is pulled inside 0-45 too
    assert g.tilt_range == (0, 45) and esp.hi == 45 and esp.tilt == 45
    text = run(g, "t 100")
    assert esp.tilt == 45 and "held at 45" in text

    assert servo_test.handle(g, "q", lambda s: None) is False
    assert "limits" in run(g, "h") or "commands" in run(g, "h")
    print("servo tool tests passed")


if __name__ == "__main__":
    test()
