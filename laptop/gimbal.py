"""Serial driver for the ESP32 pan/tilt gimbal (firmware: Arduino/gimbal/gimbal.ino).

Protocol (115200 baud, newline-terminated):
    P<0-180>   pan to angle        T<90-180>  tilt to angle
    C          center (P90 T135)   S          report status
Every accepted command is answered with "P<pan> T<tilt>"; bad input gets "ERR ...".
Servos have no position feedback, so the angles are the last values commanded.
"""
import re
import socket
import threading
import time
from collections import deque

import serial

PAN_RANGE = (0, 180)
TILT_RANGE = (90, 180)       # firmware rejects tilt below 90
CENTER = (90, 135)
_REPLY = re.compile(r"P(\d+)\s+T(\d+)")


UDP_PORT = 4210


class GimbalError(RuntimeError):
    pass


class UdpLink:
    """Serial-like wrapper (write/readline) that talks to the ESP's Wi-Fi firmware over UDP."""

    def __init__(self, host, port=UDP_PORT, timeout=0.5):
        self.addr = (host, port)
        self.timeout = timeout
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(timeout)

    def write(self, data):
        self.sock.setblocking(False)       # drop any late reply from an earlier command
        try:
            while True:
                self.sock.recvfrom(64)
        except (BlockingIOError, OSError):
            pass
        self.sock.settimeout(self.timeout)
        self.sock.sendto(data, self.addr)

    def readline(self):
        try:
            return self.sock.recvfrom(64)[0].rstrip(b"\r\n") + b"\n"
        except socket.timeout:
            return b""

    def close(self):
        self.sock.close()


def _clamp(v, lo, hi):
    return max(lo, min(hi, int(round(v))))


class Gimbal:
    def __init__(self, port=None, baud=115200, link=None, settle_s=2.0):
        """port: 'COM7' (USB serial) or 'udp:192.168.137.50' (Wi-Fi firmware).
        link: a pre-opened serial-like object (used by tests)."""
        if link is None and str(port).startswith("udp:"):
            link = UdpLink(port[4:])
        elif link is None:
            link = serial.Serial(port, baud, timeout=0.3)
            time.sleep(settle_s)          # opening the port resets the ESP32; wait for boot
            link.reset_input_buffer()
        self.link = link
        self.pan, self.tilt = CENTER

    def _send(self, cmd):
        self.link.write(cmd.encode() + b"\n")
        line = self.link.readline().decode(errors="replace").strip()
        if line.startswith("ERR"):
            raise GimbalError(f"{cmd!r}: {line}")
        m = _REPLY.fullmatch(line)
        if not m:
            raise GimbalError(f"{cmd!r}: unexpected reply {line!r}")
        self.pan, self.tilt = int(m[1]), int(m[2])
        return self.pan, self.tilt

    def status(self):
        return self._send("S")

    def center(self):
        return self._send("C")

    def move_to(self, pan=None, tilt=None):
        """Absolute move. Sends only the axes that actually change; angles are clamped to safe ranges."""
        if pan is not None and (p := _clamp(pan, *PAN_RANGE)) != self.pan:
            self._send(f"P{p}")
        if tilt is not None and (t := _clamp(tilt, *TILT_RANGE)) != self.tilt:
            self._send(f"T{t}")
        return self.pan, self.tilt

    def nudge(self, d_pan=0, d_tilt=0):
        """Relative move in degrees."""
        return self.move_to(self.pan + d_pan, self.tilt + d_tilt)

    def close(self):
        self.link.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class AsyncGimbal:
    """Non-blocking wrapper: move_to() returns at once and a background thread talks to the ESP.

    The ESP takes tens of milliseconds to answer a move; waiting for it inside the video loop would delay
    every frame. Here only the latest wanted angle per axis is kept, so a slow ESP just skips intermediate
    angles instead of queueing them. `pan`/`tilt` are the angles last requested; `error` holds the last
    communication failure (or None).
    """

    def __init__(self, port=None, **kwargs):
        self._g = Gimbal(port, **kwargs)
        self.pan, self.tilt = self._g.pan, self._g.tilt
        self.error = None
        self.rtt_ms = deque(maxlen=100)    # how long the ESP took to answer recent commands
        self._want = {"P": None, "T": None}
        self._lock = threading.Lock()      # guards _want
        self._io = threading.Lock()        # one conversation with the ESP at a time
        self._wake = threading.Event()
        self._stop = False
        threading.Thread(target=self._run, daemon=True).start()

    def move_to(self, pan=None, tilt=None):
        with self._lock:
            if pan is not None:
                self.pan = self._want["P"] = _clamp(pan, *PAN_RANGE)
            if tilt is not None:
                self.tilt = self._want["T"] = _clamp(tilt, *TILT_RANGE)
        self._wake.set()
        return self.pan, self.tilt

    def _run(self):
        while not self._stop:
            self._wake.wait(0.5)
            self._wake.clear()
            with self._lock:
                want, self._want = self._want, {"P": None, "T": None}
            for axis, value in want.items():
                if value is not None:
                    try:
                        t = time.perf_counter()
                        with self._io:
                            self._g._send(f"{axis}{value}")
                        self.rtt_ms.append((time.perf_counter() - t) * 1000)
                        self.error = None
                    except Exception as e:
                        self.error = str(e)

    def status(self):
        with self._io:
            return self._g.status()

    def center(self):
        with self._lock:
            self._want = {"P": None, "T": None}
        with self._io:
            self.pan, self.tilt = self._g.center()
        return self.pan, self.tilt

    def close(self):
        self._stop = True
        self._wake.set()
        self._g.close()


if __name__ == "__main__":
    import sys
    port = sys.argv[1] if len(sys.argv) > 1 else "COM7"
    with Gimbal(port) as g:
        print("status:", g.status())
