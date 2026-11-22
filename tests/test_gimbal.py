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


class FakeFeather:
    """Mirrors firmware/feather_m0/gimbal_pca9685: three servos (P, T, L), limp until the first command to each, O releases,
    F trims the oscillator, replies carry all three angles. No angle limits in the firmware itself."""

    def __init__(self):
        self.angle, self.energised, self.osc, self.out, self.log = [90, 22, 90], [False] * 3, 25000, b"", []

    def write(self, data):
        cmd = data.decode().strip()
        self.log.append(cmd)
        if cmd == "C":
            self.angle = [90, 22, 90]
            self.energised = [True] * 3
        elif cmd == "S":
            pass
        elif cmd == "O":
            self.energised = [False] * 3
        elif cmd == "R":
            self.out += b"R-1\n"
            return
        elif cmd == "I":
            self.out += b"I none\n"
            return
        elif cmd[:1] in "PTL" and len(cmd) > 1:
            if len(cmd) > 4 or not cmd[1:].isdigit():
                self.out += b"ERR invalid angle\n"
                return
            axis = "PTL".index(cmd[0])
            self.angle[axis] = max(0, min(180, int(cmd[1:])))
            self.energised[axis] = True
        elif cmd[:1] == "F" and len(cmd) > 1:
            if not cmd[1:].isdigit() or not 20000 <= int(cmd[1:]) <= 30000:
                self.out += b"ERR frequency out of range\n"
                return
            self.osc = int(cmd[1:])
        else:
            self.out += b"ERR unknown command\n"
            return
        self.out += f"P{self.angle[0]} T{self.angle[1]} L{self.angle[2]}\n".encode()

    def readline(self):
        line, _, self.out = self.out.partition(b"\n")
        return line + b"\n"

    def close(self):
        pass


def test_feather():
    """The laptop side against the Feather firmware's rules: limp start, first command always sent, lift servo, release."""
    f = FakeFeather()
    g = Gimbal(link=f, tilt_limits=(0, 180))
    assert g.status() == (90, 22) and g.lift == 90 and f.energised == [False] * 3, "starts limp"
    g.move_to(pan=90)                                      # the same as the assumed start angle, but it must still be sent
    assert f.energised == [True, False, False] and f.log[-1] == "P90", f.log
    g.move_to(pan=90)
    assert f.log.count("P90") == 1, "after that, an unchanged angle is not sent again"
    g.move_to(tilt=22, lift=90)                            # first commands to tilt and lift: sent even though they equal the start values
    assert f.energised == [True, True, True] and g.lift == 90
    g.move_to(lift=140)
    assert g.lift == 140 and f.angle[2] == 140 and f.log[-1] == "L140"
    g.move_to(lift=999)
    assert f.angle[2] == 180, "the lift goes up to 180 and no further"
    g.release()
    assert f.energised == [False] * 3 and f.log[-1] == "O"
    g.move_to(pan=90)
    assert f.energised[0] and f.log[-1] == "P90", "after a release the next command energises again"
    g.set_oscillator(26500)
    assert f.osc == 26500
    assert g.center() == (90, 22) and f.energised == [True] * 3
    g.move_to(pan=120, lift=30)
    assert (f.angle[0], f.angle[2]) == (120, 30) and g.pan == 120 and g.lift == 30
    for bad in ("L9999", "Lx", "F5", "Z"):                 # errors come back as ERR and raise on the laptop side
        f.write(bad.encode())
        assert f.readline().startswith(b"ERR"), bad
    old = FakeESP()                                        # the ESP32 firmware (no lift in its replies) still works
    h = Gimbal(link=old)
    assert h.status() == (90, 22) and h.lift == 90
    print("feather firmware rules ok")


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
    test_feather()
    test_udp()
    test_async()
