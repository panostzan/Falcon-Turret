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

from shrink import shrink_planes      # lives next to this file on the Pi (~/bin/shrink.py)
import sysmon                          # ~/bin/sysmon.py: live QNX stats and the real-time demo switches
import aiproc                          # ~/bin/aiproc.py: keeps the face-detection AI program running at low priority

UNIT = "5"
FPS = sys.argv[1] if len(sys.argv) > 1 else "30"
QUALITY = int(sys.argv[2]) if len(sys.argv) > 2 else 90
DISPLAY = sys.argv[3] if len(sys.argv) > 3 else "full"   # "full" = the picture as captured (640x480); "480" = 480x360 for the dashboard
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
UDP_PORT = 5006                     # the laptop sends control lines here; turretd's answers go straight back, not behind video
RECORDER = ["camera_example4_record", "-u", UNIT, "-c", "none", "-f", "ucv", "-r", FPS]


_tj = ctypes.CDLL("libturbojpeg.so.0")
_tj.tjInitCompress.restype = ctypes.c_void_p
_handle = _tj.tjInitCompress()
TJSAMP_422, TJFLAG_FASTDCT = 1, 2048


def encode(raw):
    """640x480 YUY2 -> JPEG bytes (planar 4:2:2 handed straight to libturbojpeg). With DISPLAY = "480" the picture sent to the
    laptop is shrunk to 480x360 first (the tracker on this Pi still works from the full-size picture)."""
    y, u, v = raw[0::2], raw[1::4], raw[3::4]           # Y plane 640x480, U and V planes 320x480
    width, height = SRC_W, SRC_H
    if DISPLAY == "480":
        y, u, v = shrink_planes(y, u, v, SRC_W, SRC_H)
        width, height = SRC_W * 3 // 4, SRC_H * 3 // 4
    planar = bytes(y) + bytes(u) + bytes(v)
    buf, size = ctypes.c_void_p(), ctypes.c_ulong()
    rc = _tj.tjCompressFromYUV(ctypes.c_void_p(_handle), planar, width, 4, height, TJSAMP_422,
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
        self.client = None                  # the laptop's address, learned from its first UDP datagram
        self.monitor = sysmon.Monitor(self._push_udp)    # before any thread starts: the UDP thread uses it from its first datagram
        subprocess.call(["slay", "-f", "facewatch"], stderr=subprocess.DEVNULL)
        self.face = aiproc.build(self._push_udp)         # None unless the AI program and its model are installed under ~/ai
        if self.face is not None:
            self.face.start()
        self.udp = None
        try:
            self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.udp.bind(("", UDP_PORT))
            threading.Thread(target=self._udp_control, daemon=True).start()
        except OSError:
            self.udp = None                 # no UDP channel: everything still works through the SSH stream
        for target in (self._write_frames, self._read_lines, self._forward_control, self.monitor.run):
            threading.Thread(target=target, daemon=True).start()

    def _udp_control(self):
        """Control lines from the laptop over UDP (heartbeat, arm, aim, ping...). `ping x` is answered at once with `P x`, so
        the laptop can time this path; every other line goes to turretd."""
        while True:
            try:
                data, addr = self.udp.recvfrom(512)
            except OSError:
                return
            self.client = addr
            for line in data.decode(errors="replace").splitlines():
                if line.startswith("ping "):
                    try:
                        self.udp.sendto((f"P {line[5:]} {time.monotonic() * 1000:.3f}").encode(), addr)
                    except OSError:
                        pass
                elif line and not (self.monitor.handle(line) or self._local(line)):          # stress/rt lines belong to sysmon, faceai to us, the rest to turretd
                    try:
                        self.sock.sendto(line.encode() + b"\n", CONTROL)
                    except OSError:
                        pass

    def _local(self, line):
        """Control lines for the streamer itself: 'faceai on|off' switches the face AI's pictures on or off. True if it was one."""
        if not line.startswith("faceai "):
            return False
        if self.face is not None:
            self.face.enabled = line.split()[1:] == ["on"]
        return True

    def _push_udp(self, text):
        """turretd's answer straight to the laptop, the moment it exists (not queued behind a video frame)."""
        if self.udp is not None and self.client is not None:
            try:
                self.udp.sendto(text.encode(), self.client)
            except OSError:
                pass

    def submit(self, raw):
        """Hand over the newest picture; never waits (an older picture that was not taken yet is replaced).
        Returns a marker for take(): turretd's answer to THIS picture is the first one after it."""
        if not self.ok:
            return 0
        if self.face is not None:
            self.face.submit(raw)                    # the AI gets a few frames a second, at its own low priority
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
            self._push_udp(text)            # outside the lock; the same line also rides the video stream as a fallback

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
                if line.startswith(b"ping "):       # timing probe through the SSH stream: answered inside the stream
                    with self._cv:
                        self._out.append("P " + line[5:].decode(errors="replace").strip() + f" {time.monotonic() * 1000:.3f}")
                    continue
                text = line.decode(errors="replace")
                if self.monitor.handle(text) or self._local(text):
                    continue
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
        return [l for l in lines if l.startswith("E ") or l.startswith("P ")] + status

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
        self.dead = False           # the recorder exited (no camera) or never produced a file: the main loop starts a new one

    def latest(self):
        """Return index of the newest complete frame, or -1 if none yet."""
        if self.proc.poll() is not None:                      # the recorder is gone: with no camera plugged in it exits at once
            self.dead = True
            return -1
        if not self.started and time.time() - self.born > 2:   # app waits for a keypress to begin
            try:
                self.proc.stdin.write(b"\n")
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError):
                self.dead = True
                return -1
            self.started = True
        if self.started and self.path is None and time.time() - self.born > 12:
            self.dead = True                                  # it started but never made a file: give up on this one
            return -1
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


LAST_FRAME = [time.time()]          # when the newest picture was read; the status thread watches this
STATS = {"frames": 0, "bytes": 0, "encode_s": 0.0}     # counted by the main loop, reported by the status thread


def usb_has_device():
    """True if the Pi's USB controllers list any device (a camera that fell off the bus lists nothing). Only asked when stalled."""
    try:
        out = subprocess.run(["usb"], capture_output=True, text=True, timeout=4).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return "Vendor" in out


def camera_status(brain):
    """Once a second: 'C ok <age>' while pictures flow, 'C stalled <age> usb|nousb' when none arrived for 2 s, so the dashboard can
    say WHY there is no picture (camera not detected, or detected but silent)."""
    last = (time.time(), time.process_time(), 0, 0, 0.0)
    while True:
        time.sleep(1.0)
        now, cpu = time.time(), time.process_time()
        frames, nbytes, enc = STATS["frames"], STATS["bytes"], STATS["encode_s"]
        dt = now - last[0]
        if dt > 0 and frames > last[2]:                      # V <pictures per second> <KB per picture sent> <encode ms> <this program's CPU %>
            n = frames - last[2]
            brain._push_udp(f"V {n / dt:.1f} {(nbytes - last[3]) / n / 1024:.1f} {(enc - last[4]) / n * 1000:.1f} {(cpu - last[1]) / dt * 100:.0f}")
        last = (now, cpu, frames, nbytes, enc)
        age = time.time() - LAST_FRAME[0]
        if age < 2.0:
            brain._push_udp(f"C ok {age:.1f}")
        else:
            present = usb_has_device()
            brain._push_udp(f"C stalled {age:.1f} {'usb' if present else 'nousb' if present is not None else 'unknown'}")


def main():
    # This program (video, control, monitoring) runs at priority 20: above ordinary programs (10), below the tracker (30). Under an
    # overload it stays responsive, so the dashboard can still talk to the Pi; the demo then isolates what the tracker's priority does.
    subprocess.call(["slay", "-Q", "-P", "20", str(os.getpid())], stderr=subprocess.DEVNULL)
    subprocess.call(["slay", "-f", "camera_example4_record"], stderr=subprocess.DEVNULL)
    for stale in glob.glob(DIR + "/VID_*.ucv"):
        os.remove(stale)
    out = sys.stdout.buffer
    brain = Brain()
    if brain.ok:
        threading.Thread(target=camera_status, args=(brain,), daemon=True).start()
    cur, nxt, last = Recording(), None, -1
    try:
        while True:
            # A missing or silent camera is never a reason to quit: wait, and start a fresh recorder. When the camera comes
            # back (replugged) pictures resume by themselves. Only a failed write to the laptop ends this program.
            if nxt is not None and nxt.dead:
                nxt.close()
                nxt = None
            if cur.dead or (time.time() - cur.born > 12 and time.time() - LAST_FRAME[0] > 8):
                cur.close()
                time.sleep(1.0)
                cur, nxt, last = Recording(), None, -1
                continue
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
                    LAST_FRAME[0] = time.time()
                    stamp = f"F {time.monotonic() * 1000:.3f}"       # the Pi's clock when this picture was read: the laptop measures the trip
                    marker = brain.submit(raw)
                    t_enc = time.perf_counter()
                    jpg = encode(raw)                       # turretd works on the same picture while this runs
                    STATS["encode_s"] += time.perf_counter() - t_enc
                    STATS["frames"] += 1
                    STATS["bytes"] += len(jpg)
                    said = [stamp] + (brain.take(marker, 0.02) if brain.ok else [])
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
