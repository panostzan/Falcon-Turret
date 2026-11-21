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


def _ssh_open(ip, timeout=0.6):
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
    with ThreadPoolExecutor(64) as pool:                   # not in ARP (or stale): look for every SSH host
        ips = [f"192.168.137.{i}" for i in range(2, 255)]
        hits = [ip for ip, ok in zip(ips, pool.map(_ssh_open, ips)) if ok]
    table = _arp_table()                                   # the scan just filled in the MAC addresses
    for ip in hits:
        if re.search(re.escape(ip) + r"\s+" + PI_MAC, table, re.I):
            return ip
    return hits[0] if len(hits) == 1 else FALLBACK_IP      # a single SSH host on this network is the Pi


def _spawn(quality):
    return subprocess.Popen(
        ["ssh", "-i", KEY, "-o", "MACs=hmac-sha2-256", "-o", "BatchMode=yes",
         "-o", "ServerAliveInterval=5", f"{USER}@{find_pi_ip()}", f"python3 ~/bin/pi_stream.py {FPS} {quality}"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)


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


def jpeg_frames(quality=QUALITY):
    """Yield (timestamp, JPEG bytes) forever; reconnects if the link drops."""
    while True:
        proc = _spawn(quality)
        try:
            while True:
                head = _read_exact(proc.stdout, 4)
                body = head and _read_exact(proc.stdout, struct.unpack("<I", head)[0])
                if body is None:
                    break
                yield time.time(), bytes(body)
        finally:
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
        threading.Thread(target=self._run, args=(quality,), daemon=True).start()

    def _run(self, quality):
        for ts, jpg in jpeg_frames(quality):
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
