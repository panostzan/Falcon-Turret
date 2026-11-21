"""Pi-side camera streamer (QNX). Run over SSH by laptop/receiver.py.

Drives QNX's camera_example4_record to capture raw YUY2 640x480 from camera unit 5, tails the growing
recording, JPEG-encodes (libturbojpeg, full resolution, 4:2:2) only the NEWEST frame and writes it to
stdout as [4-byte little-endian length][JPEG], so latency stays low even if the consumer is slow. Recordings rotate seamlessly: the next recorder starts and is
confirmed producing frames before the old one is stopped and deleted.

File format (reverse-engineered): 96-byte header, then frames of 640*480*2 bytes each followed by
8 extra bytes. The true frame stride is therefore 614408, not 614400.
"""
import ctypes
import glob
import os
import struct
import subprocess
import sys
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
                    jpg = encode(raw)
                    out.write(struct.pack("<I", len(jpg)) + jpg)
                    out.flush()
                    last = n
                    continue
            time.sleep(POLL_S)
    except (BrokenPipeError, KeyboardInterrupt):
        pass
    finally:
        for r in (cur, nxt):
            if r:
                r.close()


if __name__ == "__main__":
    main()
