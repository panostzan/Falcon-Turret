"""Receives the Pi's JPEG frame stream ([u32 length][JPEG]) over one persistent SSH connection."""
import os
import re
import socket
import subprocess
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor

USER = "qnxuser"
PI_MAC = "88-a2-9e-a9-a5-4e"           # the Pi's hardware address; its IP changes when the hotspot restarts
FALLBACK_IP = "192.168.137.131"
KEY = os.path.expanduser("~/.ssh/turret_ed25519")
FPS, QUALITY = 30, 90   # recorder fps, JPEG quality (lower = smaller frames, faster)


def _ssh_open(ip, timeout=1.5):
    try:
        with socket.create_connection((ip, 22), timeout=timeout):
            return True
    except OSError:
        return False


def _arp_table():
    try:
        return subprocess.run(["arp", "-a"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def find_pi_ip():
    """Find the Pi on the hotspot network. Its IP changes whenever the hotspot restarts, and Windows keeps stale ARP
    entries for old addresses, so a candidate counts only if its SSH port actually answers.
    Order: PI_HOST env var; ARP entries with the Pi's MAC that answer; a scan of the subnet; the last known address."""
    if os.environ.get("PI_HOST"):
        return os.environ["PI_HOST"]
    pattern = r"(192\.168\.137\.\d+)\s+" + PI_MAC
    for ip in re.findall(pattern, _arp_table(), re.I):
        if _ssh_open(ip):
            return ip
    hits = []
    for _attempt in range(2):                              # a busy hotspot can miss a host once: scan twice before giving up
        with ThreadPoolExecutor(64) as pool:               # not in ARP (or stale): look for every SSH host
            ips = [f"192.168.137.{i}" for i in range(2, 255)]
            hits = [ip for ip, ok in zip(ips, pool.map(_ssh_open, ips)) if ok]
        table = _arp_table()                               # the scan just filled in the MAC addresses
        for ip in hits:
            if re.search(re.escape(ip) + r"\s+" + PI_MAC, table, re.I):
                return ip
        if len(hits) == 1:
            return hits[0]                                 # a single SSH host on this network is the Pi
    return FALLBACK_IP


def _spawn(quality):
    return subprocess.Popen(
        ["ssh", "-i", KEY, "-o", "MACs=hmac-sha2-256", "-o", "BatchMode=yes",
         "-o", "ServerAliveInterval=5",
         # the Pi's IP changes, but its identity does not: trust its saved key under a fixed name, refuse any other host
         "-o", "HostKeyAlias=qnxpi", "-o", "StrictHostKeyChecking=yes", f"{USER}@{find_pi_ip()}", f"python3 ~/bin/pi_stream.py {FPS} {quality}"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)


def _read_exact(stream, n):
    """Read exactly n bytes; return None on EOF."""
    buf = bytearray(n)
    view, got = memoryview(buf), 0
    while got < n:
        k = stream.readinto(view[got:])
        if not k:
            return None
        got += k
    return buf


META_FLAG = 0x80000000   # set in a packet's length when it carries text from turretd (on QNX) instead of a JPEG
MAX_PACKET = 8 * 1024 * 1024


def parse_meta(text):
    """turretd's lines -> (status, events). status is a dict from the newest 'T' line (or None); events are (t_ms, text).
    T line: T seq t_ms found cx cy w h confidence state armed shots link proc_us scan_pan"""
    status, events = None, []
    for line in text.splitlines():
        parts = line.split()
        try:
            if parts[0] == "T" and len(parts) == 15:
                status = {"seq": int(parts[1]), "t_ms": float(parts[2]), "found": parts[3] == "1",
                          "cx": float(parts[4]), "cy": float(parts[5]), "w": float(parts[6]), "h": float(parts[7]),
                          "confidence": float(parts[8]), "state": parts[9], "armed": parts[10] == "1",
                          "shots": int(parts[11]), "link": parts[12] == "1", "proc_us": float(parts[13]),
                          "scan_pan": float(parts[14])}
            elif parts[0] == "E" and len(parts) >= 3:
                events.append((float(parts[1]), " ".join(parts[2:])))
        except (ValueError, IndexError):
            continue          # a garbled line is skipped, never fatal
    return status, events


def jpeg_frames(quality=QUALITY, on_meta=None, on_proc=None):
    """Yield (timestamp, JPEG bytes) forever; reconnects if the link drops.
    Text packets from turretd are passed to on_meta(text); on_proc(process or None) is told about each ssh process, so the
    caller can send control lines to the Pi through its stdin."""
    while True:
        proc = _spawn(quality)
        if on_proc:
            on_proc(proc)
        try:
            while True:
                head = _read_exact(proc.stdout, 4)
                if head is None:
                    break
                length = struct.unpack("<I", head)[0]
                is_meta = bool(length & META_FLAG)
                length &= ~META_FLAG
                if length > MAX_PACKET:
                    break                                    # garbage on the line: reconnect
                body = _read_exact(proc.stdout, length)
                if body is None:
                    break
                if is_meta:
                    if on_meta:
                        on_meta(bytes(body).decode(errors="replace"))
                    continue
                yield time.time(), bytes(body)
        finally:
            if on_proc:
                on_proc(None)
            proc.kill()
            proc.wait()
        time.sleep(1)   # brief backoff before reconnecting


class FrameFeed:
    """Reads the Pi's stream on its own thread and keeps only the NEWEST frame.

    If the consumer is busy, older frames are dropped instead of piling up in the network buffer, so what
    get() returns is always as fresh as possible. `dropped` counts frames that were never consumed.
    """

    def __init__(self, quality=QUALITY):
        self._cv = threading.Condition()
        self._latest, self._seq, self._consumed = None, 0, 0
        self.dropped = 0
        self.meta, self.meta_at = None, 0.0          # newest status line from turretd (QNX) and when it arrived
        self.events = []                             # (t_ms, text) from turretd, newest last
        self._proc = None
        threading.Thread(target=self._run, args=(quality,), daemon=True).start()
        threading.Thread(target=self._heartbeat, daemon=True).start()

    def _on_meta(self, text):
        status, events = parse_meta(text)
        if status:
            self.meta, self.meta_at = status, time.time()
        if events:
            self.events = (self.events + events)[-40:]

    def _on_proc(self, proc):
        self._proc = proc
        if proc is None:
            self.meta = None                          # the Pi side is gone: do not trust the last answer

    def send(self, line):
        """Send one control line to turretd on the Pi. Returns False if there is no connection."""
        proc = self._proc
        if proc is None:
            return False
        try:
            proc.stdin.write(line.encode() + b"\n")
            proc.stdin.flush()
            return True
        except (OSError, ValueError):
            return False

    def _heartbeat(self):
        """turretd disarms the turret by itself if these stop (the laptop or the link died)."""
        while True:
            self.send("hb")
            time.sleep(0.2)

    def qnx_fresh(self, max_age=0.6):
        """The newest turretd status if it is recent, else None."""
        return self.meta if (self.meta and time.time() - self.meta_at < max_age) else None

    def _run(self, quality):
        for ts, jpg in jpeg_frames(quality, self._on_meta, self._on_proc):
            with self._cv:
                if self._seq != self._consumed:     # the previous frame was never picked up
                    self.dropped += 1
                self._seq += 1
                self._latest = (ts, jpg)
                self._cv.notify_all()

    def get(self, last_seq, timeout=2.0):
        """Block until a frame newer than last_seq exists; return (seq, arrival time, jpeg) or None."""
        with self._cv:
            if not self._cv.wait_for(lambda: self._latest is not None and self._seq != last_seq, timeout):
                return None
            self._consumed = self._seq
            return (self._seq, *self._latest)


class CameraFeed:
    """Same interface as FrameFeed, but the video comes from a USB webcam plugged into THIS laptop (DirectShow index:
    0 is the built-in camera, then 1, 2 ...). Useful without the Pi, and as a fallback if the hotspot drops.
    Only the newest picture is kept; `error` says why the camera could not be used (or None)."""

    def __init__(self, index, quality=90):
        self.index, self.quality, self.error, self.dropped = index, quality, None, 0
        self.meta, self.events = None, []             # no QNX behind a laptop webcam
        self._cv = threading.Condition()
        self._latest, self._seq, self._consumed = None, 0, 0
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        import cv2
        while True:
            cap = cv2.VideoCapture(self.index, cv2.CAP_DSHOW)
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
            if not cap.isOpened():
                self.error = f"cannot open camera {self.index}: check the number and close other programs using it"
                cap.release()
                time.sleep(2)
                continue
            self.error = None
            while True:
                ok, frame = cap.read()
                if not ok:
                    self.error = f"camera {self.index} stopped delivering pictures"
                    break
                jpg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, self.quality])[1].tobytes()
                with self._cv:
                    if self._seq != self._consumed:
                        self.dropped += 1
                    self._seq += 1
                    self._latest = (time.time(), jpg)
                    self._cv.notify_all()
            cap.release()
            time.sleep(1)

    def get(self, last_seq, timeout=2.0):
        with self._cv:
            if not self._cv.wait_for(lambda: self._latest is not None and self._seq != last_seq, timeout):
                return None
            self._consumed = self._seq
            return (self._seq, *self._latest)

    def send(self, line):
        return False

    def qnx_fresh(self, max_age=0.6):
        return None


if __name__ == "__main__":
    n, t0 = 0, time.time()
    for _, jpg in jpeg_frames():
        n += 1
        if n == 1:
            open("captures/first.jpg", "wb").write(jpg)
            print("first frame:", len(jpg), "bytes")
        if n % 30 == 0:
            print(f"{n} frames, {n / (time.time() - t0):.1f} fps", flush=True)
        if n >= 150:
            break
