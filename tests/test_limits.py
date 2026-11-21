"""Tests the gimbal limits: the loader, this gimbal's real limits.json, and that aiming can never send an angle
outside them, even for absurd inputs. Run: python tests/test_limits.py"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
LAPTOP = os.path.join(HERE, "..", "laptop")
REAL = os.path.join(LAPTOP, "limits.json")
os.environ["TURRET_LIMITS"] = REAL            # these tests are about the real file
sys.path.insert(0, LAPTOP)
import gimbal   # noqa: E402


def write(data):
    f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
    json.dump(data, f)
    f.close()
    return f.name


def bad(data):
    path = write(data)
    try:
        gimbal.load_limits(path)
    except RuntimeError:
        return True
    finally:
        os.remove(path)
    return False


def test():
    # the real file: tilt travel is 0-45 degrees (the bracket's stops), pan the full 0-180, home inside both
    pan, tilt, center = gimbal.load_limits(REAL)
    assert pan == (0, 180) and tilt == (0, 45), (pan, tilt)
    assert tilt[0] <= center[1] <= tilt[1] and pan[0] <= center[0] <= pan[1]
    assert (gimbal.PAN_RANGE, gimbal.TILT_RANGE, gimbal.CENTER) == (pan, tilt, center)

    # the loader refuses anything unsafe or malformed: no silent defaults
    assert bad({"pan": [0, 180], "tilt": [0, 45]}), "missing home position"
    assert bad({"pan": [0, 180], "tilt": [0, 200], "center": [90, 22]}), "range beyond 180"
    assert bad({"pan": [0, 180], "tilt": [-5, 45], "center": [90, 22]}), "negative minimum"
    assert bad({"pan": [0, 180], "tilt": [45, 0], "center": [90, 22]}), "min above max"
    assert bad({"pan": [0, 180], "tilt": [0, 45], "center": [90, 60]}), "home outside the tilt range"
    assert bad({"pan": "wide", "tilt": [0, 45], "center": [90, 22]}), "wrong types"
    try:
        gimbal.load_limits(os.path.join(HERE, "does_not_exist.json"))
        raise SystemExit("a missing limits file must be an error")
    except RuntimeError:
        pass

    # run the real aiming code with the REAL limits against extreme inputs; record every command it would send
    probe = r'''
import os, sys, random
sys.path.insert(0, os.path.join(%r))
from aim import Aimer
from gimbal import PAN_RANGE, TILT_RANGE, CENTER, Gimbal
class Rec:
    def __init__(self): self.pan, self.tilt = CENTER; self.sent = []
    def move_to(self, pan=None, tilt=None):
        self.pan = self.pan if pan is None else pan; self.tilt = self.tilt if tilt is None else tilt
        self.sent.append((self.pan, self.tilt)); return self.pan, self.tilt
rnd = random.Random(1)
for mode in ("map", "follow"):
    for sign in (1, -1):
        g = Rec(); a = Aimer(g, mode=mode); a.sign.update(pan=sign, tilt=sign)
        for i in range(400):
            e = (rnd.choice([-1, 1]) * rnd.uniform(0, 5), rnd.choice([-1, 1]) * rnd.uniform(0, 5))   # even off-screen errors
            a.update(*e)
        bad = [c for c in g.sent if not (PAN_RANGE[0] <= c[0] <= PAN_RANGE[1] and TILT_RANGE[0] <= c[1] <= TILT_RANGE[1])]
        assert not bad, (mode, sign, bad[:3])
# the same through the real Gimbal driver (its own clamp is the last software layer)
class Link:
    def __init__(self): self.log = []; self.out = b"P90 T22\r\n"
    def write(self, d): self.log.append(d.decode().strip()); self.out = b"P90 T22\r\n"
    def readline(self): return self.out
    def close(self): pass
link = Link(); g = Gimbal(link=link)
for t in (-50, 0, 10, 45, 46, 90, 135, 180, 400):
    g.move_to(tilt=t)
tilts = [int(c[1:]) for c in link.log if c.startswith("T")]
assert tilts and all(0 <= t <= 45 for t in tilts), tilts
for p in (-10, 0, 90, 180, 500):
    g.move_to(pan=p)
pans = [int(c[1:]) for c in link.log if c.startswith("P")]
assert pans and all(0 <= p <= 180 for p in pans), pans
g.center()
assert link.log[-2:] == ["P90", "T22"], link.log[-2:]
print("ok")
''' % (LAPTOP,)
    env = {k: v for k, v in os.environ.items()}
    env["TURRET_LIMITS"] = REAL
    r = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, env=env)
    assert r.returncode == 0 and r.stdout.strip() == "ok", r.stdout + r.stderr
    print("gimbal limits tests passed (no angle ever left pan 0-180 / tilt 0-45)")


if __name__ == "__main__":
    test()
