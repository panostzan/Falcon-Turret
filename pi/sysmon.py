"""Runs ON THE PI next to pi_stream.py: the live QNX picture for the dashboard, plus the two switches of the real-time demo.

  Monitor   every ~2 s runs QNX's own `top` once, boils it down to one line ("Q {json}": CPU, memory, the busiest programs with
            their scheduling priorities) and hands it to pi_stream, which sends it to the laptop over the fast UDP channel.
  Stress    `stress on` starts a pile of CPU-burning programs at the normal priority (10) so the Pi is genuinely overloaded;
            `stress off` stops them. They also stop by themselves after 90 s and whenever the streamer exits.
  priority  `rt on` raises the tracker (turretd) to real-time priority 30, `rt off` puts it back to the normal 10. This is the
            whole trick the demo shows: under load the priority-30 tracker keeps its timing, everything at 10 slows down.

Nothing here changes the system: only our own programs' priorities are touched, and only for as long as they run.
"""
import atexit
import json
import re
import subprocess
import threading
import time

HOGS = 8                      # more than the 4 cores, so the machine is really busy
HOG_MAX_SECONDS = 90          # safety: stress never outlives this
RT_PRIORITY, NORMAL_PRIORITY = 30, 10
TRACKER = "turretd"

# One overload program: burns a CPU, and ends by itself after HOG_MAX_SECONDS + 5 or as soon as its parent is gone (so a crashed
# streamer can never leave them running).
HOG_CODE = chr(10).join([
    "import os, time",
    "parent, end, i = os.getppid(), time.time() + %d, 0" % (HOG_MAX_SECONDS + 5),
    "while True:",
    "    i += 1",
    "    if i % 100000 == 0 and (os.getppid() != parent or time.time() > end):",
    "        break",
])

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_ROW = re.compile(r"^\s*(\d+)\s+(\d+)\s+(\d+)\s+(\S+)\s+(\d+:\d+:\d+)\s+([\d.]+)%\s+(.+?)\s*$")
_USAGE = re.compile(r"CPU states:\s*([\d.]+)% user,\s*([\d.]+)% kernel")
_MEM = re.compile(r"Memory:\s*(\d+)M total,\s*(\d+)M avail")
_COUNTS = re.compile(r"(\d+) online CPUs; (\d+) processes; (\d+) threads")


def parse_top(text):
    """QNX `top -b` output -> {"cpus", "processes", "threads", "user", "kernel", "mem_total", "mem_avail", "threads_list"}.
    Missing parts come back as None / []; this never raises on odd output."""
    text = _ANSI.sub("", text)
    out = {"cpus": None, "processes": None, "threads": None, "user": None, "kernel": None, "mem_total": None, "mem_avail": None, "rows": []}
    m = _COUNTS.search(text)
    if m:
        out["cpus"], out["processes"], out["threads"] = int(m.group(1)), int(m.group(2)), int(m.group(3))
    m = _USAGE.search(text)
    if m:
        out["user"], out["kernel"] = float(m.group(1)), float(m.group(2))
    m = _MEM.search(text)
    if m:
        out["mem_total"], out["mem_avail"] = int(m.group(1)), int(m.group(2))
    for line in text.splitlines():
        m = _ROW.match(line)
        if m:
            out["rows"].append({"pid": int(m.group(1)), "tid": int(m.group(2)), "prio": int(m.group(3)), "state": m.group(4),
                                "cpu": float(m.group(6)), "name": m.group(7)})
    return out


def summarize(parsed, limit=6):
    """The one-line report: totals plus the busiest programs (a program's threads are added together; its priority is its highest)."""
    procs = {}
    for r in parsed["rows"]:
        p = procs.setdefault(r["pid"], {"name": r["name"], "pid": r["pid"], "prio": r["prio"], "cpu": 0.0})
        p["cpu"] += r["cpu"]
        p["prio"] = max(p["prio"], r["prio"]) if r["name"] != "kernel" else p["prio"]
    top = sorted(procs.values(), key=lambda p: -p["cpu"])[:limit]
    tracker = next((p for p in procs.values() if p["name"] == TRACKER), None)
    used = None if parsed["mem_total"] is None else parsed["mem_total"] - parsed["mem_avail"]
    return {"cpus": parsed["cpus"], "procs": parsed["processes"], "threads": parsed["threads"],
            "cpu": None if parsed["user"] is None else round(parsed["user"] + parsed["kernel"], 1),
            "mem_used": used, "mem_total": parsed["mem_total"],
            "tracker_prio": tracker["prio"] if tracker else None,
            "top": [{"name": p["name"], "pid": p["pid"], "prio": p["prio"], "cpu": round(p["cpu"], 1)} for p in top]}


class Stress:
    def __init__(self):
        self.procs = []
        self.lock = threading.Lock()
        self.stop_at = 0.0
        atexit.register(self.stop)

    def active(self):
        with self.lock:
            return bool(self.procs)

    def start(self):
        with self.lock:
            if self.procs:                            # already running: starting again only renews the safety timer
                self.stop_at = time.time() + HOG_MAX_SECONDS
                return
            for _ in range(HOGS):
                try:
                    # `on -p 10`: the streamer itself runs at 20, and children inherit that; the overload must be at the NORMAL priority
                    self.procs.append(subprocess.Popen(["on", "-p", str(NORMAL_PRIORITY), "python3", "-c", HOG_CODE], stdin=subprocess.DEVNULL,
                                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
                except OSError:
                    break
            self.stop_at = time.time() + HOG_MAX_SECONDS

    def stop(self):
        with self.lock:
            procs, self.procs = self.procs, []
        for p in procs:
            try:
                p.kill()
                p.wait(2)
            except Exception:
                pass

    def check_timer(self):
        if self.procs and time.time() > self.stop_at:
            self.stop()


def set_tracker_priority(prio):
    """slay -P <prio> turretd: the tracker's scheduling priority (all of its threads). Returns True on success."""
    return subprocess.call(["slay", "-Q", "-f", "-P", str(prio), TRACKER], stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL) == 0


class Monitor:
    def __init__(self, push):
        self.push = push                              # push(text): sends a line to the laptop over UDP
        self.stress = Stress()
        self.rt_wanted = True

    def handle(self, line):
        """A control line meant for this module ('stress on|off', 'rt on|off'). Returns True when it was one of ours."""
        parts = line.split()
        if len(parts) != 2 or parts[0] not in ("stress", "rt") or parts[1] not in ("on", "off"):
            return False
        on = parts[1] == "on"
        if parts[0] == "stress":
            self.stress.start() if on else self.stress.stop()
        else:
            self.rt_wanted = on
            set_tracker_priority(RT_PRIORITY if on else NORMAL_PRIORITY)
        return True

    def run(self):
        """Loop forever: make sure the tracker has the wanted priority, sample `top`, report."""
        time.sleep(3.0)                               # let turretd start first
        set_tracker_priority(RT_PRIORITY if self.rt_wanted else NORMAL_PRIORITY)
        while True:
            self.stress.check_timer()
            try:
                raw = subprocess.run(["top", "-b", "-D", "1", "-i", "1", "-z", "30"], capture_output=True, timeout=10).stdout
                s = summarize(parse_top(raw.decode(errors="replace")))
                s["stress"] = len(self.stress.procs)
                s["rt"] = self.rt_wanted
                self.push("Q " + json.dumps(s, separators=(",", ":")))
            except Exception:
                pass
            time.sleep(1.0)
