"""The system check, run against pretend hardware. Run: python tests/test_syscheck.py"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "laptop"))
import syscheck  # noqa: E402

fails = 0


def check(name, ok, detail=""):
    global fails
    print(("PASS " if ok else "FAIL ") + name + (" - " + detail if detail else ""))
    fails += 0 if ok else 1


class Feed:
    def rtt(self):
        return {"udp": {"n": 20, "p50": 3.0, "p95": 9.0}, "stream": {"n": 5, "p50": 60.0, "p95": 300.0}}


class G:
    def __init__(self, fail=None):
        self.pan, self.tilt, self.lift, self.moves, self.closed, self.fail = 90, 22, 90, [], False, fail

    def status(self):
        if self.fail:
            raise RuntimeError(self.fail)
        return self.pan, self.tilt

    def center(self):
        self.moves.append("C")
        return 90, 22

    def move_to(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)
        self.moves.append(kw)

    def close(self):
        self.closed = True


def env(**over):
    base = {"feed": lambda: Feed(), "camera": lambda: {"state": "ok"}, "frame_age": lambda: 0.1,
            "qnx": lambda: {"present": True, "link": True, "armed": False, "proc_ms": 8.1, "state": "IDLE"},
            "tracking": lambda: False, "held_gimbal": lambda: G(), "open_gimbal": lambda: G(),
            "laptop": lambda: {"ram_free_gb": 8.0}, "depth": lambda: {"on": False}}
    base.update(over)
    return base


def by(res, name):
    return next(r for r in res["rows"] if r["name"] == name)


g0 = G()
r = syscheck.run(env(open_gimbal=lambda: g0))
check("everything healthy: verdict pass, servo test skipped", r["summary"]["verdict"] == "pass" and by(r, "Servos")["state"] == "skip", str(r["summary"]))
check("no wiggle unless asked: no servo was moved", g0.moves == [])

g = G()
r = syscheck.run(env(open_gimbal=lambda: g), wiggle=True)
check("wiggle: all three servos moved and went back", by(r, "Servos")["state"] == "pass" and g.moves[0] == "C" and {"pan", "tilt", "lift"} <= {k for m in g.moves[1:] for k in m} and g.closed,
      f"{len(g.moves)} commands")
check("wiggle: ends where it started", (g.pan, g.tilt, g.lift) == (90, 22, 90), str((g.pan, g.tilt, g.lift)))
r = syscheck.run(env(tracking=lambda: True), wiggle=True)
check("wiggle refuses while tracking holds the Feather", by(r, "Servos")["state"] == "skip")

r = syscheck.run(env(camera=lambda: {"state": "stalled", "usb": False}, frame_age=lambda: 30.0))
check("camera missing from USB: red with a fix", by(r, "Camera")["state"] == "fail" and "USB" in by(r, "Camera")["fix"] and r["summary"]["verdict"] == "fail")
r = syscheck.run(env(qnx=lambda: {"present": False}))
check("tracker silent: red", by(r, "QNX tracker")["state"] == "fail" and by(r, "Fire interlock")["state"] == "skip")
r = syscheck.run(env(qnx=lambda: {"present": True, "link": True, "armed": True, "proc_ms": 8, "state": "LOCK"}))
check("armed turret: amber warning", by(r, "Fire interlock")["state"] == "warn" and r["summary"]["verdict"] == "warn")
r = syscheck.run(env(open_gimbal=lambda: G(fail="could not open port 'COM8': PermissionError(13, 'Access is denied.')")))
check("Feather port held elsewhere: amber, says to close the sliders", by(r, "Feather and servo driver")["state"] == "warn" and "sliders" in by(r, "Feather and servo driver")["fix"])


def boom():
    raise OSError("could not open port 'COM8': FileNotFoundError")


r = syscheck.run(env(open_gimbal=boom))
check("Feather unplugged: red", by(r, "Feather and servo driver")["state"] == "fail")
r = syscheck.run(env(feed=lambda: None))
check("no Pi connection: red", by(r, "Pi link")["state"] == "fail")


class Slow(Feed):
    def rtt(self):
        return {"udp": {"n": 20, "p50": 50.0, "p95": 200.0}}


r = syscheck.run(env(feed=lambda: Slow()))
check("slow control link: amber", by(r, "Pi link")["state"] == "warn")


def crash():
    raise RuntimeError("boom")


r = syscheck.run(env(camera=crash))
check("a crashing check becomes a red row, not an exception", r["summary"]["verdict"] == "fail" and len(r["rows"]) == 7)
print("\nALL PASSED" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
