"""Receives the Pi's JPEG frame stream ([u32 length][JPEG]) over one persistent SSH connection."""
import json
import os
import re
import socket
import subprocess
import struct
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

USER = "qnxuser"
def _pi_mac():
    """The Pi's hardware address (its IP changes when the hotspot restarts). Kept out of the repo: put it in laptop/pi_mac.txt
    (git-ignored, like 88-aa-bb-cc-dd-ee) or in the TURRET_PI_MAC environment variable."""
    v = os.environ.get("TURRET_PI_MAC", "")
    if not v:
        try:
            v = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "pi_mac.txt")).read()
        except OSError:
            v = ""
    return v.strip().lower().replace(":", "-") or "00-00-00-00-00-00"


PI_MAC = _pi_mac()
FALLBACK_IP = "192.168.137.131"
KEY = os.path.expanduser("~/.ssh/turret_ed25519")
FPS, QUALITY = 30, int(os.environ.get("TURRET_QUALITY", "65"))   # recorder fps, JPEG quality (lower = smaller frames, faster)
DISPLAY = os.environ.get("TURRET_DISPLAY", "480")   # picture size sent for display: "480" = 480x360, "full" = 640x480 (the Pi's tracker always uses the full picture)


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


current_ip = [None]          # the Pi address the newest SSH connection used (the UDP channel talks to the same one)
UDP_PORT = 5006


def _spawn(quality):
    current_ip[0] = find_pi_ip()
    return subprocess.Popen(
        ["ssh", "-i", KEY, "-o", "MACs=hmac-sha2-256", "-o", "BatchMode=yes",
         "-o", "ServerAliveInterval=5",
         # the Pi's IP changes, but its identity does not: trust its saved key under a fixed name, refuse any other host
         "-o", "HostKeyAlias=qnxpi", "-o", "StrictHostKeyChecking=yes", f"{USER}@{current_ip[0]}", f"python3 ~/bin/pi_stream.py {FPS} {quality} {DISPLAY}"],
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


ERROR_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "captures", "feed_errors.log")


def log_error(where, exc):
    """Record an unexpected error from the video reader (captures/feed_errors.log) instead of letting a thread die silently."""
    try:
        os.makedirs(os.path.dirname(ERROR_LOG), exist_ok=True)
        with open(ERROR_LOG, "a", encoding="utf-8") as f:
            import traceback
            f.write(f"{time.strftime('%H:%M:%S')} {where}: {exc!r}\n{traceback.format_exc()}\n")
    except OSError:
        pass


META_FLAG = 0x80000000   # set in a packet's length when it carries text from turretd (on QNX) instead of a JPEG
MAX_PACKET = 8 * 1024 * 1024


def parse_pongs(text):
    """The 'P <id> [<Pi clock ms>]' lines (answers to timing probes): a list of (id, Pi clock in ms or None)."""
    out = []
    for line in text.splitlines():
        p = line.split()
        if len(p) >= 2 and p[0] == "P":
            try:
                out.append((p[1], float(p[2]) if len(p) > 2 else None))
            except ValueError:
                out.append((p[1], None))
    return out


def parse_stamp(text):
    """The Pi's clock (ms) from an 'F <ms>' line that travels just ahead of each picture, or None."""
    for line in text.splitlines():
        p = line.split()
        if len(p) == 2 and p[0] == "F":
            try:
                return float(p[1])
            except ValueError:
                return None
    return None


def parse_system(text):
    """The Pi's system report ('Q {json}': CPU, memory, busiest programs with their priorities, stress/real-time switches), or None."""
    for line in text.splitlines():
        if line.startswith("Q {"):
            try:
                d = json.loads(line[2:])
                return d if isinstance(d, dict) else None
            except ValueError:
                return None
    return None


def percentile(values, q):
    values = sorted(values)
    return values[min(len(values) - 1, int(len(values) * q))] if values else None


def parse_faces(text):
    """The face program's report ('FACE <count> <ms> [x y w h score]...', boxes as fractions of the picture), or None."""
    for line in text.splitlines():
        p = line.split()
        if len(p) >= 3 and p[0] == "FACE":
            try:
                n, ms = int(p[1]), float(p[2])
                vals = [float(v) for v in p[3:]]
            except ValueError:
                return None
            return {"n": n, "ms": ms, "boxes": [tuple(vals[i:i + 5]) for i in range(0, len(vals) - 4, 5)]}
    return None


def parse_video_stats(text):
    """The Pi's own video measurements ('V <pictures/s> <KB per picture> <encode ms> <CPU %>'), or None."""
    for line in text.splitlines():
        p = line.split()
        if len(p) == 5 and p[0] == "V":
            try:
                return {"fps": float(p[1]), "kb": float(p[2]), "encode_ms": float(p[3]), "cpu_pct": float(p[4])}
            except ValueError:
                return None
    return None


def parse_camera(text):
    """The camera status line from the Pi ('C ok 0.1' or 'C stalled 5.3 usb|nousb|unknown'), or None. usb is True/False/None."""
    for line in text.splitlines():
        p = line.split()
        if len(p) >= 3 and p[0] == "C" and p[1] in ("ok", "stalled"):
            try:
                return {"state": p[1], "age": float(p[2]), "usb": (p[3] == "usb") if len(p) > 3 and p[3] in ("usb", "nousb") else None}
            except ValueError:
                return None
    return None


def parse_meta(text):
    """turretd's lines -> (status, events). status is a dict from the newest 'T' line (or None); events are (t_ms, text).
    T line: T seq t_ms found cx cy w h confidence state armed shots link proc_us scan_pan"""
    status, events = None, []
    for line in text.splitlines():
        parts = line.split()
        try:
            if parts[0] == "T" and len(parts) in (15, 19):
                status = {"seq": int(parts[1]), "t_ms": float(parts[2]), "found": parts[3] == "1",
                          "cx": float(parts[4]), "cy": float(parts[5]), "w": float(parts[6]), "h": float(parts[7]),
                          "confidence": float(parts[8]), "state": parts[9], "armed": parts[10] == "1",
                          "shots": int(parts[11]), "link": parts[12] == "1", "proc_us": float(parts[13]),
                          "scan_pan": float(parts[14]),
                          "gate": len(parts) == 19 and parts[15] == "1", "human": len(parts) == 19 and parts[16] == "1",
                          "ai_ok": len(parts) == 19 and parts[17] == "1", "face_ms": float(parts[18]) if len(parts) == 19 else -1.0}
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
                        try:
                            on_meta(bytes(body).decode(errors="replace"))
                        except Exception as e:          # a bad status line must never take the video down
                            log_error("meta handler", e)
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
        self._pings = {}                             # probe id -> time sent
        self.camera, self.camera_at = None, 0.0       # the Pi's report on its camera (None until it says something)
        self.pi_video, self.pi_video_at = None, 0.0   # the Pi's own video measurements (rate, size, encode time, CPU)
        self.pi_system, self.pi_system_at = None, 0.0  # the Pi's system report (CPU, memory, programs, priorities)
        self.faces, self.faces_at = None, 0.0          # the AI face program's newest report (count, ms, boxes)
        self.proc_log = deque(maxlen=900)             # (arrival time, tracker processing time in ms) per answer: the control loop's timing
        self._arrivals = deque(maxlen=600)            # (arrival time, bytes, delay in ms or None) of each picture at THIS end of the Wi-Fi
        self._offsets = deque(maxlen=40)              # (round trip s, laptop clock minus Pi clock s) from the UDP pings
        self._next_stamp = None                       # the Pi clock stamp of the picture about to arrive
        self._meta_cv = threading.Condition()        # signalled when a newer turretd status arrives
        self.rtt_ms = {"udp": deque(maxlen=120), "stream": deque(maxlen=120)}
        self._probe_n = 0
        self._udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._udp.settimeout(1.0)
        self._udp.bind(("", 0))                      # bound now, so the listener thread can read before the first send
        self.udp_answers = 0                         # how many answers arrived by UDP (0 = that channel is not working)
        threading.Thread(target=self._run, args=(quality,), daemon=True).start()
        threading.Thread(target=self._heartbeat, daemon=True).start()
        threading.Thread(target=self._udp_loop, daemon=True).start()
        threading.Thread(target=self._probe_loop, daemon=True).start()

    def _on_meta(self, text, via="stream"):
        status, events = parse_meta(text)
        # an answer counts only if it is newer than the one we have: the same line arrives twice (UDP first, then the stream)
        if status and (self.meta is None or status["t_ms"] >= self.meta["t_ms"]):
            self.meta, self.meta_at = status, time.time()
            self.proc_log.append((self.meta_at, status["proc_us"] / 1000.0))
            with self._meta_cv:
                self._meta_cv.notify_all()
        if events:
            known = {e for e in self.events}
            self.events = (self.events + [e for e in events if e not in known])[-40:]
        cam = parse_camera(text)
        if cam:
            self.camera, self.camera_at = cam, time.time()
        pv = parse_video_stats(text)
        if pv:
            self.pi_video, self.pi_video_at = pv, time.time()
        ps = parse_system(text)
        if ps:
            self.pi_system, self.pi_system_at = ps, time.time()
        fc = parse_faces(text)
        if fc:
            self.faces, self.faces_at = fc, time.time()
        now = time.time()
        stamp = parse_stamp(text)
        if stamp is not None:
            self._next_stamp = stamp
        for pid, pi_ms in parse_pongs(text):
            sent = self._pings.pop(pid, None)
            if sent is not None:
                self.rtt_ms[via if via in self.rtt_ms else "stream"].append((now - sent) * 1000)
                if via == "udp" and pi_ms is not None:               # clock offset: the Pi's reading was taken about half an RTT ago
                    self._offsets.append((now - sent, (sent + now) / 2 - pi_ms / 1000.0))

    def _udp_loop(self):
        """Answers that come straight back over UDP (tracker status the moment it exists, timing probes)."""
        while True:
            try:
                data, addr = self._udp.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                return
            self.udp_answers += 1
            self._on_meta(data.decode(errors="replace"), via="udp")

    def _probe_loop(self):
        """Timing probes, twice a second: one through the UDP channel and one through the SSH video stream, so the dashboard
        can show how long each takes to come back."""
        while True:
            time.sleep(0.5)
            if self._proc is None:
                continue
            self._probe_n += 1
            for tag, path in (("u", "udp"), ("s", "ssh")):
                pid = f"{tag}{self._probe_n}"
                self._pings[pid] = time.time()
                self.send(f"ping {pid}", path)
            for old in [p for p, t in self._pings.items() if time.time() - t > 5]:
                del self._pings[old]                 # lost probes are forgotten, not counted

    def _delay_ms(self, arrival):
        """How long this picture took from the Pi reading it to arriving here, using the Pi's stamp and the clock offset measured
        with the quickest recent UDP round trip (the most symmetric one). None until both exist."""
        stamp, self._next_stamp = self._next_stamp, None
        if stamp is None or not self._offsets:
            return None
        best = min(self._offsets)                                    # lowest round trip = most trustworthy offset
        return (arrival - (best[1] + stamp / 1000.0)) * 1000.0

    def tracker_trace(self, n=150):
        """The tracker's time per picture (ms) for the last n answers, oldest first."""
        return [round(v, 2) for _, v in list(self.proc_log)[-n:]]

    def tracker_timing(self, window=10.0):
        """How long the QNX tracker took per picture over the last `window` seconds (ms): median, 95th percentile, worst, and the count."""
        now = time.time()
        vals = [v for t, v in list(self.proc_log) if now - t <= window]
        if len(vals) < 3:
            return None
        return {"p50": round(percentile(vals, 0.5), 2), "p95": round(percentile(vals, 0.95), 2), "max": round(max(vals), 2), "n": len(vals)}

    def wire_stats(self, window=30.0):
        """What actually arrived over the Wi-Fi in the last `window` seconds: pictures per second, KB per picture, and the gaps
        between pictures (median, 95th, 99th percentile, worst, and how many were over 200 ms). None before there is enough data."""
        now = time.time()
        pts = [p for p in list(self._arrivals) if now - p[0] <= window]
        if len(pts) < 5:
            return None
        delays = sorted(p[2] for p in pts if p[2] is not None and -50 < p[2] < 5000)
        gaps = sorted((b[0] - a[0]) * 1000 for a, b in zip(pts, pts[1:]))
        pick = lambda q: round(gaps[min(len(gaps) - 1, int(len(gaps) * q))], 1)
        span = pts[-1][0] - pts[0][0]
        return {"n": len(pts), "fps": round((len(pts) - 1) / span, 1) if span > 0 else 0.0,
                "kb": round(sum(p[1] for p in pts) / len(pts) / 1024, 1), "gap_p50": pick(0.5), "gap_p95": pick(0.95),
                "gap_p99": pick(0.99), "gap_max": round(gaps[-1], 1), "over_200": sum(1 for g in gaps if g > 200),
                "delay_p50": round(delays[len(delays) // 2], 1) if delays else None,
                "delay_p95": round(delays[min(len(delays) - 1, int(len(delays) * 0.95))], 1) if delays else None}

    def rtt(self):
        """Round-trip times of the timing probes in ms: {path: {p50, p95, n}} for the UDP channel and the SSH stream."""
        out = {}
        for path, values in self.rtt_ms.items():
            v = sorted(values)
            out[path] = {"n": len(v), "p50": round(v[len(v) // 2], 1) if v else None,
                         "p95": round(v[min(len(v) - 1, int(len(v) * 0.95))], 1) if v else None}
        return out

    def _on_proc(self, proc):
        self._proc = proc
        if proc is None:
            self.meta = None                          # the Pi side is gone: do not trust the last answer

    def send(self, line, path="both"):
        """Send one control line to the Pi, over the UDP channel, the SSH stream or both (default: both, since every line is
        harmless to receive twice and a datagram can be lost). Returns False if there is no connection at all."""
        proc, sent = self._proc, False
        if proc is None:
            return False
        if path in ("both", "udp") and current_ip[0]:
            try:
                self._udp.sendto(line.encode() + b"\n", (current_ip[0], UDP_PORT))
                sent = True
            except OSError:
                pass
        if path in ("both", "ssh"):
            try:
                proc.stdin.write(line.encode() + b"\n")
                proc.stdin.flush()
                sent = True
            except (OSError, ValueError):
                pass
        return sent

    def _heartbeat(self):
        """turretd disarms the turret by itself if these stop (the laptop or the link died)."""
        while True:
            self.send("hb")
            time.sleep(0.2)

    def wait_meta(self, after_t_ms, timeout=0.5):
        """Block until a turretd status newer than `after_t_ms` (the Pi's own clock) exists; return it, or None on timeout. The
        aiming loop runs on this, so it reacts the moment the Pi's answer arrives and does not wait for a video frame."""
        with self._meta_cv:
            self._meta_cv.wait_for(lambda: self.meta is not None and self.meta["t_ms"] > after_t_ms, timeout)
        m = self.meta
        return m if (m is not None and m["t_ms"] > after_t_ms) else None

    def qnx_fresh(self, max_age=0.3):
        """The newest turretd status if it is recent (about 4 pictures), else None: then the laptop tracker takes over."""
        return self.meta if (self.meta and time.time() - self.meta_at < max_age) else None

    def _run(self, quality):
        while True:                                  # whatever goes wrong, the reader keeps going: log it and reconnect
            try:
                for ts, jpg in jpeg_frames(quality, self._on_meta, self._on_proc):
                    self._arrivals.append((ts, len(jpg), self._delay_ms(ts)))
                    with self._cv:
                        if self._seq != self._consumed:     # the previous frame was never picked up
                            self.dropped += 1
                        self._seq += 1
                        self._latest = (ts, jpg)
                        self._cv.notify_all()
                return                               # the stream ended by itself (real streams never do; test streams do)
            except Exception as e:
                log_error("video reader", e)
                self._proc = None
                time.sleep(1)

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
        self.camera, self.camera_at = None, 0.0
        self.pi_video, self.pi_video_at = None, 0.0
        self.pi_system, self.pi_system_at = None, 0.0
        self.proc_log = deque(maxlen=10)
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

    def send(self, line, path="both"):
        return False

    def rtt(self):
        return {}

    def wire_stats(self, window=30.0):
        return None

    def tracker_timing(self, window=10.0):
        return None

    def tracker_trace(self, n=150):
        return []

    def wait_meta(self, after_t_ms, timeout=0.5):
        time.sleep(timeout)
        return None

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
