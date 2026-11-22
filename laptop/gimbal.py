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
import os
import time
from collections import deque

import serial

PAN_RANGE = (0, 180)
TILT_RANGE = (0, 180)        # what the firmware accepts. The usable range is smaller and is a SOFTWARE limit: see tilt_limits below
TILT_HOME = 22               # the firmware's start position for tilt
LIFT_RANGE = (0, 150)        # the lift (platform) servo is never sent above 150 degrees. A SOFTWARE limit: see lift_limits below
CENTER = (90, TILT_HOME)


def default_lift_limits():
    """The software limit on the lift servo: laptop/lift_calibration.json if the operator saved one with tools/servo_sliders.py
    (Mark lift LOW / HIGH), else 0-150."""
    import json
    path = os.environ.get("TURRET_LIFT_CALIBRATION") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "lift_calibration.json")
    try:
        with open(path, encoding="utf-8") as f:
            cal = json.load(f)
        lo, hi = int(cal["low"]), int(cal["high"])
        if 0 <= lo < hi <= 180:
            return (lo, hi)
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return LIFT_RANGE


def default_tilt_limits():
    """The software limit on tilt: the calibration made with tools/tilt_watch.py, else 0-45. Nothing in the firmware limits it."""
    import tilt_calibration
    window, _ = tilt_calibration.load()
    return window or (0, 45)
_REPLY = re.compile(r"P(\d+)\s+T(\d+)(?:\s+L(\d+))?")      # the Feather firmware adds the lift (platform) servo: L<angle>


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
    def __init__(self, port=None, baud=115200, link=None, settle_s=2.0, tilt_limits=None, lift_limits=None):
        """port: 'COM8' (USB serial, the Feather M0) or 'udp:192.168.137.50' (Wi-Fi firmware).
        link: a pre-opened serial-like object (used by tests).
        tilt_limits: (low, high) the tilt is never commanded outside. Default: the saved calibration, else 0-45. Pass
        TILT_RANGE for no limit (the calibration tool does)."""
        self.tilt_limits = tuple(tilt_limits) if tilt_limits else default_tilt_limits()
        self.lift_limits = tuple(lift_limits) if lift_limits else default_lift_limits()   # the lift never goes outside this
        if link is None and str(port).startswith("udp:"):
            link = UdpLink(port[4:])
        elif link is None:
            link = serial.Serial(port, baud, timeout=0.3)
            time.sleep(settle_s)          # opening the port resets the ESP32; wait for boot
            link.reset_input_buffer()
        self.link = link
        self.pan, self.tilt = CENTER
        self.lift = 90                    # the lift / platform servo (third servo; only the Feather firmware has it)
        self._sent = set()                # axes commanded since this connection opened: the FIRST command to each is always sent,
                                          # because the Feather's servos are limp until commanded, even for an angle we 'already have'

    def _send(self, cmd):
        self.link.write(cmd.encode() + b"\n")
        line = self.link.readline().decode(errors="replace").strip()
        if line.startswith("ERR"):
            raise GimbalError(f"{cmd!r}: {line}")
        m = _REPLY.fullmatch(line)
        if not m:
            raise GimbalError(f"{cmd!r}: unexpected reply {line!r}")
        self.pan, self.tilt = int(m[1]), int(m[2])
        if m[3] is not None:
            self.lift = int(m[3])
        return self.pan, self.tilt

    def status(self):
        return self._send("S")

    def center(self):
        reply = self._send("C")
        self._sent.update("PTL")
        return reply

    def _query(self, cmd, pattern):
        """Send a non-movement command whose reply is not a position; returns the regex match."""
        self.link.write(cmd.encode() + b"\n")
        line = self.link.readline().decode(errors="replace").strip()
        m = re.fullmatch(pattern, line)
        if not m:
            raise GimbalError(f"{cmd!r}: unexpected reply {line!r}")
        return m

    def distance_mm(self):
        """Latest ToF distance in millimetres, or None when there is no sensor or no valid reading."""
        mm = int(self._query("R", r"R(-?\d+)")[1])
        return None if mm < 0 else mm

    def sensor_model(self):
        """'VL53L0X' / 'VL53L1X', or None when no distance sensor was found."""
        name = self._query("I", r"I (\S+)")[1]
        return None if name == "none" else name

    def move_to(self, pan=None, tilt=None, lift=None):
        """Absolute move. Sends only the axes that actually change (and always the first command to an axis, which is what
        energises a limp servo); angles are clamped to safe ranges. `lift` is the platform servo (software limit 0-150)."""
        if pan is not None and ((p := _clamp(pan, *PAN_RANGE)) != self.pan or "P" not in self._sent):
            self._send(f"P{p}")
            self._sent.add("P")
        if tilt is not None and ((t := _clamp(tilt, *self.tilt_limits)) != self.tilt or "T" not in self._sent):
            self._send(f"T{t}")
            self._sent.add("T")
        if lift is not None and ((l := _clamp(lift, *self.lift_limits)) != self.lift or "L" not in self._sent):
            self._send(f"L{l}")
            self._sent.add("L")
        return self.pan, self.tilt

    def release(self):
        """Let every servo go limp (the Feather firmware's O command). The next move to an axis energises it again."""
        self._send("O")
        self._sent.clear()

    def set_oscillator(self, khz):
        """Trim the servo driver's oscillator (Feather firmware, F command), in kHz: 25000 is nominal. If every angle reads a few
        degrees off in the same proportion, this is the knob."""
        self._send(f"F{int(khz)}")

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
                self.tilt = self._want["T"] = _clamp(tilt, *self._g.tilt_limits)
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

    def distance_mm(self):
        with self._io:
            return self._g.distance_mm()

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
    port = sys.argv[1] if len(sys.argv) > 1 else "COM8"
    with Gimbal(port) as g:
        print("status:", g.status())
