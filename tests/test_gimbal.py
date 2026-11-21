"""Tests Gimbal against a simulated ESP32 that follows the firmware's protocol (no hardware needed).
Run: python tests/test_gimbal.py"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "laptop"))
from gimbal import Gimbal, GimbalError   # noqa: E402


class FakeESP:
    """Mirrors gimbal.ino's command handling."""

    def __init__(self):
        self.pan, self.tilt, self.out, self.log = 90, 135, b"", []

    def write(self, data):
        cmd = data.decode().strip()
        self.log.append(cmd)
        if cmd == "C":
            self.pan, self.tilt = 90, 135
        elif cmd == "S":
            pass
        elif cmd[:1] in "PT" and len(cmd) > 1 and cmd[1:].isdigit() and len(cmd) <= 4:
            v = int(cmd[1:])
            if cmd[0] == "P":
                self.pan = max(0, min(180, v))
            else:
                self.tilt = max(90, min(180, v))
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
    assert g.status() == (90, 135)
    assert g.move_to(pan=120, tilt=160) == (120, 160)
    assert g.center() == (90, 135)
    try:
        g._send("X")
        raise SystemExit("expected GimbalError")
    except GimbalError:
        pass
    g.close()
    srv.close()
    print("udp gimbal tests passed")


def test():
    esp = FakeESP()
    g = Gimbal(link=esp)
    assert g.status() == (90, 135)
    assert g.move_to(pan=120) == (120, 135)
    assert g.move_to(tilt=200) == (120, 180), "tilt clamps to 180"
    assert g.move_to(tilt=10) == (120, 90), "tilt clamps to 90 (firmware minimum)"
    assert g.move_to(pan=-50) == (0, 90), "pan clamps to 0"
    assert g.nudge(d_pan=10, d_tilt=5) == (10, 95)
    esp.log.clear()
    g.move_to(pan=10, tilt=95)
    assert esp.log == [], "no-op moves send nothing"
    assert g.center() == (90, 135)
    try:
        g._send("X")
        raise SystemExit("expected GimbalError")
    except GimbalError:
        pass
    print("all gimbal tests passed")


if __name__ == "__main__":
    test()
    test_udp()
