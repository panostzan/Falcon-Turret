"""Tests Gimbal against a simulated ESP32 that follows the firmware's protocol (no hardware needed).
Run: python tests/test_gimbal.py"""
import os
os.environ["TURRET_TILT_CALIBRATION"] = "none.json"   # never read the operator's real calibration in a test
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "laptop"))
from gimbal import Gimbal, GimbalError   # noqa: E402


class FakeESP:
    """Mirrors gimbal.ino's command handling."""

    def __init__(self):
        self.pan, self.tilt, self.out, self.log = 90, 22, b"", []

    def write(self, data):
        cmd = data.decode().strip()
        self.log.append(cmd)
        if cmd == "C":
            self.pan, self.tilt = 90, 22
        elif cmd == "S":
            pass
        elif cmd[:1] in "PT" and len(cmd) > 1 and cmd[1:].isdigit() and len(cmd) <= 4:
            v = int(cmd[1:])
            if cmd[0] == "P":
                self.pan = max(0, min(180, v))
            else:
                self.tilt = max(0, min(180, v))
        else:
            self.out += b"ERR unknown command\n" if cmd[:1] not in "PT" else b"ERR invalid angle\n"
            return
        self.out += f"P{self.pan} T{self.tilt}\n".encode()

    def readline(self):
        line, _, self.out = self.out.partition(b"\n")
        return line + b"\n"

    def close(self):
        pass


def test_udp():
    """Same checks, but over a real UDP socket with a simulated ESP (as the Wi-Fi firmware behaves)."""
    import socket
    import threading
    srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    srv.bind(("127.0.0.1", 0))
    srv.settimeout(3)
    port = srv.getsockname()[1]
    esp = FakeESP()

    def serve():
        try:
            while True:
                data, addr = srv.recvfrom(64)
                esp.write(data)
                srv.sendto(esp.readline().rstrip(b"\n"), addr)
        except OSError:
            pass
    threading.Thread(target=serve, daemon=True).start()
    from gimbal import UdpLink
    g = Gimbal(link=UdpLink("127.0.0.1", port, timeout=1))
    assert g.status() == (90, 22)
    assert g.move_to(pan=120, tilt=40) == (120, 40)
    assert g.center() == (90, 22)
    try:
        g._send("X")
        raise SystemExit("expected GimbalError")
    except GimbalError:
        pass
    g.close()
    srv.close()
    print("udp gimbal tests passed")


def test_async():
    """AsyncGimbal never blocks the caller, coalesces to the latest angle, and eventually delivers it."""
    import time
    from gimbal import AsyncGimbal

    class SlowESP(FakeESP):
        def write(self, data):
            time.sleep(0.04)                      # like the real ESP: ~40 ms per move
            super().write(data)

    esp = SlowESP()
    g = AsyncGimbal(link=esp)
    t0 = time.perf_counter()
    for i in range(30):                           # 30 rapid pan requests while the ESP is slow
        g.move_to(pan=91 + i)
    elapsed = (time.perf_counter() - t0) * 1000
    assert elapsed < 30, f"move_to must not wait for the ESP ({elapsed:.0f} ms for 30 calls)"
    assert g.pan == 120
    time.sleep(0.5)
    assert esp.pan == 120, f"ESP should end at the latest angle, got {esp.pan}"
    assert len(esp.log) < 30, f"slow ESP should skip intermediate angles, got {len(esp.log)} commands"
    g.move_to(pan=500, tilt=0)                    # clamped, like the sync driver
    time.sleep(0.2)
    assert (esp.pan, esp.tilt) == (180, 0) and g.error is None
    assert g.center() == (90, 22)
    g.close()
    print("async gimbal tests passed")


def test_no_limit():
    """The firmware has no tilt limit: the software limit is whatever the operator calibrated, or none at all."""
    esp = FakeESP()
    g = Gimbal(link=esp, tilt_limits=(0, 180))
    assert g.move_to(tilt=170) == (90, 170) and g.move_to(tilt=500) == (90, 180) and g.move_to(tilt=-5) == (90, 0)
    g2 = Gimbal(link=FakeESP(), tilt_limits=(10, 40))
    assert g2.move_to(tilt=100) == (90, 40) and g2.move_to(tilt=0) == (90, 10)
    print("no-limit and custom-limit tests passed")


def test():
    test_no_limit()
    esp = FakeESP()
    g = Gimbal(link=esp)
    assert g.tilt_limits == (0, 45), g.tilt_limits
    assert g.status() == (90, 22)
    assert g.move_to(pan=120) == (120, 22)
    assert g.move_to(tilt=200) == (120, 45), "tilt clamps to the software limit (0-45 by default)"
    assert g.move_to(tilt=-10) == (120, 0), "tilt clamps to 0"
    assert g.move_to(pan=-50) == (0, 0), "pan clamps to 0"
    assert g.nudge(d_pan=10, d_tilt=5) == (10, 5)
    esp.log.clear()
    g.move_to(pan=10, tilt=5)
    assert esp.log == [], "no-op moves send nothing"
    assert g.center() == (90, 22)
    try:
        g._send("X")
        raise SystemExit("expected GimbalError")
    except GimbalError:
        pass
    print("all gimbal tests passed")


if __name__ == "__main__":
    test()
    test_udp()
    test_async()
