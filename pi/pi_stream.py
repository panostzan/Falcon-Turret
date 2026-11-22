"""Pi-side camera streamer (QNX). Run over SSH by laptop/receiver.py.

Drives QNX's camera_example4_record to capture raw YUY2 640x480 from camera unit 5, tails the growing
recording, JPEG-encodes (libturbojpeg, full resolution, 4:2:2) only the NEWEST frame and writes it to
stdout as [4-byte little-endian length][JPEG], so latency stays low even if the consumer is slow. Recordings rotate seamlessly: the next recorder starts and is
confirmed producing frames before the old one is stopped and deleted.

If ~/turret_c/turretd exists, every picture is also handed to it (the C tracker + fire interlock + watchdog that run on QNX),
its answers are sent to the laptop as "meta" packets (length with the top bit set, then text lines), and control lines
arriving on this program's stdin (including the laptop's heartbeat) are forwarded to it. Without turretd the stream
works exactly as before.

File format (reverse-engineered): 96-byte header, then frames of 640*480*2 bytes each followed by
8 extra bytes. The true frame stride is therefore 614408, not 614400.
"""
import ctypes
import glob
import os
import socket
import struct
import subprocess
import sys
import threading
import time

UNIT = "5"
FPS = sys.argv[1] if len(sys.argv) > 1 else "30"
QUALITY = int(sys.argv[2]) if len(sys.argv) > 2 else 90
POLL_S = 0.012                      # file poll interval (polling too often slows the recorder)
ROTATE_S = 20                       # start a replacement recorder this often
DIR = "/data/share/sensor"
SRC_W, SRC_H = 640, 480
FRAME = SRC_W * SRC_H * 2           # bytes of pixel data per frame (YUY2)
STRIDE = FRAME + 8                  # bytes between frame starts in the file
HEADER = 96
TURRETD = os.path.expanduser("~/turret_c/turretd")
CONTROL = ("127.0.0.1", 5005)       # turretd's control port (localhost only)
META_FLAG = 0x80000000              # set in the length field of a packet that carries text, not a JPEG
RECORDER = ["camera_example4_record", "-u", UNIT, "-c", "none", "-f", "ucv", "-r", FPS]


_tj = ctypes.CDLL("libturbojpeg.so.0")
_tj.tjInitCompress.restype = ctypes.c_void_p
_handle = _tj.tjInitCompress()
TJSAMP_422, TJFLAG_FASTDCT = 1, 2048


def encode(raw):
    """640x480 YUY2 -> JPEG bytes (planar 4:2:2 handed straight to libturbojpeg)."""
    planar = raw[0::2] + raw[1::4] + raw[3::4]          # Y plane, U plane, V plane
    buf, size = ctypes.c_void_p(), ctypes.c_ulong()
    rc = _tj.tjCompressFromYUV(ctypes.c_void_p(_handle), planar, SRC_W, 4, SRC_H, TJSAMP_422,
                               ctypes.byref(buf), ctypes.byref(size), QUALITY, TJFLAG_FASTDCT)
    if rc != 0:
        raise RuntimeError("JPEG encode failed")
    jpg = ctypes.string_at(buf, size.value)
    _tj.tjFree(buf)
    return jpg


class Brain:
    """turretd, running next to this program. Optional: self.ok is False when it is not installed or will not start."""

    def __init__(self):
        self.ok = False
        self._cv = threading.Condition()
        self._frame = None
        self._out = []
        self._gen = 0                # how many status lines turretd has produced so far
        self._last_t_at = 0.0        # when the newest one arrived
        if not os.path.exists(TURRETD):
            return
        subprocess.call(["slay", "-f", "turretd"], stderr=subprocess.DEVNULL)
        try:
            self.proc = subprocess.Popen([TURRETD, str(SRC_W), str(SRC_H), str(CONTROL[1])], stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        except OSError:
            return
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.ok = True
        for target in (self._write_frames, self._read_lines, self._forward_control):
            threading.Thread(target=target, daemon=True).start()

    def submit(self, raw):
        """Hand over the newest picture; never waits (an older picture that was not taken yet is replaced).
        Returns a marker for take(): turretd's answer to THIS picture is the first one after it."""
        if not self.ok:
            return 0
        with self._cv:
            self._frame = raw
            self._cv.notify_all()
            return self._gen

    def _write_frames(self):
        while True:
            with self._cv:
                self._cv.wait_for(lambda: self._frame is not None)
                raw, self._frame = self._frame, None
            try:
                self.proc.stdin.write(raw)
            except (BrokenPipeError, OSError):
                self.ok = False
                return

    def _read_lines(self):
        for line in iter(self.proc.stdout.readline, b""):
            with self._cv:
                text = line.decode(errors="replace").rstrip()
                self._out.append(text)
                del self._out[:-300]
                if text.startswith("T "):
                    self._gen += 1
                    self._last_t_at = time.time()
                    self._cv.notify_all()

    def _forward_control(self):
        """Lines from the laptop (over SSH) go to turretd. When the laptop goes away they stop, and turretd's own
        watchdog disarms the turret."""
        buf = b""
        while True:
            try:
                chunk = os.read(0, 4096)          # the raw descriptor: no buffered-reader lock to trip over at shutdown
            except OSError:
                return
            if not chunk:
                return
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                try:
                    self.sock.sendto(line + b"\n", CONTROL)
                except OSError:
                    pass

    def take(self, after=None, wait=0.0):
        """Everything turretd said since the last call, reduced to its newest status line and all its events.
        With `after` (from submit) it first waits up to `wait` seconds for the answer to that picture, so the status that
        travels with a JPEG describes that JPEG and not the one before. It does not wait if turretd has gone quiet."""
        with self._cv:
            if wait > 0 and after is not None and time.time() - self._last_t_at < 0.5:
                self._cv.wait_for(lambda: self._gen > after, timeout=wait)
            lines, self._out = self._out, []
        status = [l for l in lines if l.startswith("T ")][-1:]
        return [l for l in lines if l.startswith("E ")] + status

    def close(self):
        if self.ok or getattr(self, "proc", None):
            try:
                self.proc.kill()
            except OSError:
                pass


class Recording:
    """One camera_example4_record process writing one .ucv file."""

    def __init__(self):
        self._known = set(glob.glob(DIR + "/VID_*.ucv"))
        self.proc = subprocess.Popen(RECORDER, stdin=subprocess.PIPE,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.born = time.time()
        self.started = False        # sent the 'start recording' keypress
        self.path = None
        self.fd = None              # unbuffered read handle on the recording
        self.ready_at = None        # when the first full frame was available

    def latest(self):
        """Return index of the newest complete frame, or -1 if none yet."""
        if not self.started and time.time() - self.born > 2:   # app waits for a keypress to begin
            self.proc.stdin.write(b"\n")
            self.proc.stdin.flush()
            self.started = True
        if self.started and self.path is None:
            new = set(glob.glob(DIR + "/VID_*.ucv")) - self._known
            self.path = new.pop() if new else None
        if self.path is None:
            return -1
        n = (os.path.getsize(self.path) - HEADER - FRAME) // STRIDE
        if n >= 0 and self.ready_at is None:
            self.ready_at = time.time()
        return n

    def read(self, n):
        if self.fd is None:
            self.fd = os.open(self.path, os.O_RDONLY)
        return os.pread(self.fd, FRAME, HEADER + n * STRIDE)

    def close(self):
        try:
            self.proc.kill()
        except OSError:
            pass
        self.proc.wait()
        if self.fd is not None:
            os.close(self.fd)
        if self.path:
            try:
                os.remove(self.path)
            except OSError:
                pass


def main():
    subprocess.call(["slay", "-f", "camera_example4_record"], stderr=subprocess.DEVNULL)
    for stale in glob.glob(DIR + "/VID_*.ucv"):
        os.remove(stale)
    out = sys.stdout.buffer
    brain = Brain()
    cur, nxt, last = Recording(), None, -1
    try:
        while True:
            n = cur.latest()
            if nxt is None and cur.ready_at and time.time() - cur.ready_at > ROTATE_S:
                nxt = Recording()
            if nxt is not None and nxt.latest() >= 0:      # replacement is producing: switch over
                cur.close()
                cur, nxt, last = nxt, None, -1
                continue
            if n > last:
                raw = cur.read(n)
                if len(raw) == FRAME:
                    marker = brain.submit(raw)
                    jpg = encode(raw)                       # turretd works on the same picture while this runs
                    said = brain.take(marker, 0.02) if brain.ok else []
                    if said:
                        text = "\n".join(said).encode()
                        out.write(struct.pack("<I", META_FLAG | len(text)) + text)
                    out.write(struct.pack("<I", len(jpg)) + jpg)
                    out.flush()
                    last = n
                    continue
            time.sleep(POLL_S)
    except (BrokenPipeError, KeyboardInterrupt):
        pass
    finally:
        brain.close()
        for r in (cur, nxt):
            if r:
                r.close()


if __name__ == "__main__":
    main()
