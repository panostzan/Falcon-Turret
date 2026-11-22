"""The roof wired into the dashboard server (no hardware: a fake gimbal). Run: python tests/test_app_roof.py"""
import json
import os
import sys
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "laptop"))
import app   # noqa: E402
import roof as R   # noqa: E402
from engage import Engagement   # noqa: E402

R.ROOF_MOVE_S = 0.9      # the checks below wait fixed times: a roof that moves at this speed keeps them short (the real default is slower)


class FakeGimbal:
    def __init__(self):
        self.moves, self.flushed, self.closed = [], 0, False

    def move_to(self, pan=None, tilt=None, lift=None, aux=None):
        self.moves.append({k: v for k, v in (("pan", pan), ("tilt", tilt), ("lift", lift), ("aux", aux)) if v is not None})
        return 90, 22

    def flush(self, timeout=1.5):
        self.flushed += 1
        return True

    def close(self):
        self.closed = True


class FakeAimer:
    def __init__(self):
        self.gimbal, self.homed, self.resets = FakeGimbal(), 0, 0

    def home(self):
        self.homed += 1

    def reset(self):
        self.resets += 1


def merged(moves):
    """Boil a move list down to one entry per servo (the angle it ended on), in the order each first moved."""
    final = {}
    for m in moves:
        for axis, v in m.items():
            if axis in ("lift", "aux"):
                final[axis] = v
    return [{a: v} for a, v in final.items()]


def call(port, path, method="GET"):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method, data=b"" if method == "POST" else None)
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read() or b"{}")


def install_roof(hibernating=False):
    """What set_tracking(True) does, with a fake gimbal."""
    aimer = FakeAimer()
    app.ctl.update(aimer=aimer, tracking=True, error=None)
    app.ctl["roof"] = R.Roof(lambda **kw: aimer.gimbal.move_to(**kw), on_close=lambda: app.roof_closing(aimer), hibernating=hibernating)
    return aimer, app.ctl["roof"]


def drive(roof, seconds, seen):
    """Run the roof for real seconds, the way roof_loop and the aim loop do."""
    end = time.time() + seconds
    while time.time() < end:
        app.roof_observe(time.time(), seen)
        roof.update(time.time())
        time.sleep(0.02)


def test():
    server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        app.engage = Engagement()
        app.ctl.update(roof=None, aimer=None, tracking=False)
        app.roof_cfg["hibernate"] = False

        # tracking off: no roof, nothing is gated, and the state says so
        assert app.roof_ready() is True
        st = call(port, "/state")
        assert st["roof"]["present"] is False and st["roof"]["state"] is None, st["roof"]

        # tracking on: the roof starts at rest (platform down first, then the roof), dormant, so nothing may aim or arm
        aimer, roof = install_roof()
        roof.start(time.time())
        drive(roof, 1.5, seen=False)
        assert merged(aimer.gimbal.moves) == [{"lift": 0}, {"aux": 140}], aimer.gimbal.moves
        assert roof.state == R.DORMANT and app.roof_ready() is False
        assert call(port, "/state")["roof"]["state"] == "DORMANT"
        assert call(port, "/arm?on=1", "POST").get("refused") and not app.engage.armed, "cannot arm with the roof closed"
        assert call(port, "/fire", "POST").get("refused"), "cannot fire with the roof closed"

        # a sure sighting wakes it; arming still needs a person
        aimer.gimbal.moves.clear()
        drive(roof, 1.6, seen=True)
        assert roof.state == R.ACTIVE and app.roof_ready() is True
        assert merged(aimer.gimbal.moves) == [{"aux": 20}, {"lift": 150}], aimer.gimbal.moves
        assert not app.engage.armed, "waking never arms"
        call(port, "/arm?on=1", "POST")
        assert app.engage.armed, "a person can arm once the roof is open"
        s = call(port, "/state")["roof"]
        assert s["present"] and s["state"] == "ACTIVE" and s["sleep_in"] is not None

        # Hibernate: closes (disarming and parking first), then rests whatever is in view
        homed = aimer.homed
        aimer.gimbal.moves.clear()
        out = call(port, "/hibernate?on=1", "POST")
        assert out["hibernate"] is True and app.roof_cfg["hibernate"] is True
        assert not app.engage.armed and aimer.homed == homed + 1, "closing disarms and parks pan/tilt"
        drive(roof, 1.6, seen=True)
        assert roof.state == R.HIBERNATE and not roof.can_aim
        assert merged(aimer.gimbal.moves) == [{"lift": 0}, {"aux": 140}], aimer.gimbal.moves
        n = len(aimer.gimbal.moves)
        drive(roof, 2.0, seen=True)
        assert len(aimer.gimbal.moves) == n and roof.state == R.HIBERNATE, "hibernating: nothing moves, nothing wakes"
        assert call(port, "/arm?on=1", "POST").get("refused") and not app.engage.armed

        # back to dormant, then it wakes normally again
        out = call(port, "/hibernate?on=0", "POST")
        assert out["hibernate"] is False and roof.state == R.DORMANT
        drive(roof, 1.8, seen=True)
        assert roof.state == R.ACTIVE

        # stopping tracking closes up in order and waits for the Feather before letting go of the port
        aimer.gimbal.moves.clear()
        t0 = time.time()
        assert call(port, "/track?on=0", "POST")["error"] is None
        assert app.ctl["roof"] is None and app.ctl["aimer"] is None
        assert merged(aimer.gimbal.moves) == [{"lift": 0}, {"aux": 140}], aimer.gimbal.moves
        assert aimer.gimbal.flushed >= 1 and aimer.gimbal.closed, "the commands were flushed before the port was released"
        assert time.time() - t0 < 3.5, "stopping does not hang"
        assert not app.engage.armed

        # Hibernate pressed while tracking is off is remembered and applied when tracking starts
        call(port, "/hibernate?on=1", "POST")
        assert app.roof_cfg["hibernate"] is True and call(port, "/state")["roof"]["hibernate"] is True
        aimer, roof = install_roof(hibernating=app.roof_cfg["hibernate"])
        roof.start(time.time())
        drive(roof, 2.0, seen=True)
        assert roof.state == R.HIBERNATE and not app.roof_ready(), "tracking started while hibernating: stays shut"
        app.ctl.update(roof=None, aimer=None, tracking=False)
        app.roof_cfg["hibernate"] = False
    finally:
        server.shutdown()
    print("app roof wiring tests passed")


if __name__ == "__main__":
    test()
