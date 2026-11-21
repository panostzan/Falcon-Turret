"""Receives the Pi's JPEG frame stream ([u32 length][JPEG]) over one persistent SSH connection."""
import os
import re
import subprocess
import struct
import time

USER = "qnxuser"
PI_MAC = "88-a2-9e-a9-a5-4e"           # the Pi's hardware address; its IP changes when the hotspot restarts
FALLBACK_IP = "192.168.137.131"
KEY = os.path.expanduser("~/.ssh/turret_ed25519")
FPS, QUALITY = 30, 90   # recorder fps, JPEG quality (lower = smaller frames, faster)


def find_pi_ip():
    """Look the Pi up by MAC in the laptop's ARP table (PI_HOST env var overrides)."""
    if os.environ.get("PI_HOST"):
        return os.environ["PI_HOST"]
    try:
        table = subprocess.run(["arp", "-a"], capture_output=True, text=True, timeout=5).stdout
        m = re.search(r"(192\.168\.137\.\d+)\s+" + PI_MAC, table, re.I)
        if m:
            return m[1]
    except (OSError, subprocess.SubprocessError):
        pass
    return FALLBACK_IP


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
