"""Manual mode (the Servos page) wired into the dashboard server. No hardware: a fake gimbal. Run: python tests/test_app_manual.py"""
import json
import os
import sys
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "laptop"))
import app   # noqa: E402
import manual as MM   # noqa: E402
import roof as R   # noqa: E402
from engage import Engagement   # noqa: E402

R.ROOF_MOVE_S = 0.9      # the checks below wait fixed times: a roof that moves at this speed keeps them short (the real default is slower)


class FakeGimbal:
    def __init__(self):
        self.moves, self.flushed, self.closed, self.statuses = [], 0, False, 0

    def move_to(self, **kw):
        self.moves.append(kw)

    def status(self):
        self.statuses += 1
        return 90, 22

    def flush(self, timeout=1.5):
        self.flushed += 1
        return True

    def close(self):
        self.closed = True


class FakeAimer:
    def __init__(self):
        self.gimbal, self.homed = FakeGimbal(), 0

    def home(self):
        self.homed += 1

    def reset(self):
        pass


def call(port, path, method="GET"):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method, data=b"" if method == "POST" else None)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read() or b"{}")


def wait_for(cond, seconds=5.0):
    end = time.time() + seconds
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test():
    server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=app.manual_loop, daemon=True).start()
    made = []

    def fake_open():
        g = FakeGimbal()
        made.append(g)
        return g
    real_open, app.open_async_gimbal = app.open_async_gimbal, fake_open
    try:
        app.engage = Engagement()
        app.ctl.update(roof=None, aimer=None, tracking=False)
        app.manual.update(on=False, ctl=None, gimbal=None, error=None)

        # the page and the data it needs
        page = call_text(port, "/servos")
        assert "Manual control" in page and "/servos.json" in page and "Servos" in call_text(port, "/")
        off = call(port, "/servos.json")
        assert off["on"] is False and off["ranges"]["roof"] == [20, 140] and off["ranges"]["lift"] == [0, 150]
        assert off["rest"] == {"pan": 90, "tilt": 22, "lift": 0, "roof": 140}, off["rest"]
        assert call(port, "/state")["manual"] == {"on": False, "parking": False}
        assert call(port, "/servo?axis=pan&angle=100", "POST")["error"], "sliders do nothing while manual mode is off"

        # entering: the server opens the Feather itself, checks it answers, and sends NOTHING yet
        out = call(port, "/manual?on=1", "POST")
        assert out["on"] is True and out["error"] is None, out
        g = made[-1]
        assert g.statuses == 1 and g.moves == [], "it asked the Feather for its status and moved nothing"
        assert call(port, "/state")["manual"]["on"] is True
        assert app.roof_ready() is False, "no aiming, arming or firing in manual mode"
        assert "manual" in call(port, "/arm?on=1", "POST")["refused"] and not app.engage.armed
        assert "manual" in call(port, "/fire", "POST")["refused"]
        assert "Manual mode is on" in call(port, "/track?on=1", "POST")["error"], "tracking cannot start in manual mode"

        # sliders: pan moves slowly toward its target
        r = call(port, "/servo?axis=pan&angle=120", "POST")
        assert r["blocked"] is None and r["target"]["pan"] == 120
        assert wait_for(lambda: call(port, "/servos.json")["sent"]["pan"] == 120)
        pans = [m["pan"] for m in g.moves if "pan" in m]
        assert pans[0] == 90 + MM.MAX_STEP and max(b - a for a, b in zip(pans, pans[1:])) <= MM.MAX_STEP, pans

        # the platform cannot rise while the roof is closed
        r = call(port, "/servo?axis=lift&angle=100", "POST")
        assert r["blocked"] and "roof" in r["blocked"].lower() and r["target"]["lift"] <= 10, r
        # open the roof, then the platform may rise a little
        assert call(port, "/servo?axis=roof&angle=90", "POST")["blocked"] is None
        assert wait_for(lambda: call(port, "/servos.json")["sent"]["roof"] == 90, 8)
        assert call(port, "/servo?axis=lift&angle=40", "POST")["blocked"] is None
        assert wait_for(lambda: call(port, "/servos.json")["sent"]["lift"] == 40, 8)
        r = call(port, "/servo?axis=roof&angle=140", "POST")
        assert r["blocked"] and "platform" in r["blocked"].lower(), "the roof will not close onto a raised platform"
        assert call(port, "/servo?axis=pan&angle=abc", "POST")["error"] == "bad angle"
        assert max(m["aux"] for m in g.moves if "aux" in m) <= 140 and min(m["aux"] for m in g.moves if "aux" in m) >= 90

        # back to Auto: parks in a safe order, waits, then lets go of the port
        g.moves.clear()
        out = call(port, "/manual?on=0", "POST")
        assert out["error"] is None and out["on"] is False, out
        assert g.closed and g.flushed >= 1, "the commands were flushed before the port was released"
        assert app.manual["ctl"] is None and app.manual["gimbal"] is None
        lifts = [i for i, m in enumerate(g.moves) if "lift" in m]
        roofs = [i for i, m in enumerate(g.moves) if m.get("aux", 0) > 100]
        assert g.moves[lifts[-1]]["lift"] == 0 and g.moves[roofs[-1]]["aux"] == 140, "platform down and roof closed"
        assert min(roofs) > lifts[0], "the platform starts down before the roof closes past 'open enough'"
        assert app.roof_ready() is True, "back to Auto: nothing is blocked any more"
        assert call(port, "/state")["manual"]["on"] is False

        # a Feather that does not answer: manual mode does not start
        def broken():
            raise OSError("could not open port 'COM8'")
        app.open_async_gimbal = broken
        out = call(port, "/manual?on=1", "POST")
        assert out["on"] is False and "COM8" in out["error"], out
        app.open_async_gimbal = fake_open

        # entering manual mode while tracking: tracking stops first and the roof closes properly, then manual starts
        aimer = FakeAimer()
        app.ctl.update(aimer=aimer, tracking=True, error=None)
        app.ctl["roof"] = R.Roof(lambda **kw: aimer.gimbal.move_to(**kw), on_close=lambda: app.roof_closing(aimer))
        app.ctl["roof"].start(time.time())
        t0 = time.time()
        while time.time() - t0 < 1.6:
            app.ctl["roof"].update(time.time())
            time.sleep(0.02)
        app.ctl["roof"]._begin_open(time.time())
        t0 = time.time()
        while time.time() - t0 < 1.4:
            app.ctl["roof"].update(time.time())
            time.sleep(0.02)
        assert app.ctl["roof"].state == R.ACTIVE
        aimer.gimbal.moves.clear()
        call(port, "/arm?on=1", "POST")
        assert app.engage.armed
        out = call(port, "/manual?on=1", "POST")
        assert out["on"] is True and app.ctl["roof"] is None and app.ctl["aimer"] is None and not app.ctl["tracking"]
        assert aimer.gimbal.closed and not app.engage.armed, "tracking stopped, roof closed, disarmed, port handed over"
        assert [m for m in aimer.gimbal.moves if "lift" in m][0]["lift"] == 0
        call(port, "/manual?on=0", "POST")
        assert app.manual["on"] is False
    finally:
        app.open_async_gimbal = real_open
        app.manual.update(on=False, ctl=None, gimbal=None, error=None)
        app.ctl.update(roof=None, aimer=None, tracking=False)
        server.shutdown()
    print("app manual-mode wiring tests passed")


def call_text(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
        return r.read().decode()


if __name__ == "__main__":
    test()
