"""Tests the HTTP endpoints (telemetry, arm, auto-scan) with an in-process server. Run: python tests/test_server.py"""
import json
import os
import sys
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "laptop"))
import app   # noqa: E402
from engage import Engagement   # noqa: E402


def call(port, path, method="GET"):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method, data=b"" if method == "POST" else None)
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.read()


def test():
    server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        app.engage = Engagement()
        for i in range(30):                                       # some telemetry
            app.telemetry.add(i / 15, age_ms=40 + i, proc_ms=3.0, pan=90 + i, tilt=135, speed=10.0)
        classic = call(port, "/classic").decode()                 # the previous dashboard is still served
        assert "ARM" in classic and "/telemetry" in classic and "Mission" in classic
        live, diag = call(port, "/").decode(), call(port, "/diagnostics").decode()
        assert "Mission" in live and "Hold to arm" in live and "/ui.js" in live
        assert "/telemetry" in diag and "Recent events" in diag
        assert b"--teal" in call(port, "/ui.css") and b"function poll" in call(port, "/ui.js")
        sysinfo = json.loads(call(port, "/system"))
        assert sysinfo["uptime_s"] >= 0 and "python" in sysinfo

        t = json.loads(call(port, "/telemetry"))
        assert set(t["series"]) == {"interval_ms", "age_ms", "proc_ms", "pan", "tilt", "speed"}
        assert t["stats"]["age_ms"]["n"] >= 30 and t["stats"]["age_ms"]["p50"] > 0
        assert isinstance(t["events"], list) and "servo_rtt_ms" in t

        assert not app.engage.armed
        call(port, "/arm?on=1", "POST")
        assert app.engage.armed
        call(port, "/arm?on=0", "POST")
        assert not app.engage.armed
        call(port, "/autoscan?on=1", "POST")
        assert app.engage.auto_scan
        call(port, "/autoscan?on=0", "POST")
        assert not app.engage.auto_scan

        call(port, "/arm?on=1", "POST")                           # tracking off always disarms
        assert json.loads(call(port, "/track?on=0", "POST"))["error"] is None
        assert not app.engage.armed
        events = json.loads(call(port, "/telemetry"))["events"]
        assert any("ARMED" in e[1] for e in events) and any("DISARMED" in e[1] for e in events), events
    finally:
        server.shutdown()
    print("server endpoint tests passed")


if __name__ == "__main__":
    test()
