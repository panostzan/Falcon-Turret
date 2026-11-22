"""Runs ON THE PI inside pi_stream.py: keeps the face-detection program (qnx/ai/facewatch, QNX's TensorFlow Lite port plus an open-source
face model) running next to the tracker.

  * it starts facewatch at a LOW priority (10), so it can never delay the tracker (30) or the streamer (20);
  * it hands it the newest camera frame a few times a second (not every frame: the model does not need them all);
  * it forwards facewatch's "FACE ..." lines to the laptop so the dashboard can draw the boxes;
  * if facewatch dies it starts it again, but gives up for a minute after repeated crashes (a crash loop must not eat the CPU).

facewatch itself tells turretd "human 0|1": this module is only the supervisor. Without the program or the model installed, nothing
here does anything and the rest of the streamer works exactly as before.
"""
import os
import subprocess
import threading
import time

FACE_HZ = 3.0                      # frames handed to the model per second
MAX_CRASHES, CRASH_WINDOW_S, PAUSE_S = 5, 60.0, 60.0


def library_dirs(root):
    """Every directory under `root` holding shared libraries (the QNX TensorFlow Lite port builds several)."""
    dirs = []
    for d, _, files in os.walk(root):
        if any(".so" in f for f in files):
            dirs.append(d)
    return dirs


class FaceWatch:
    def __init__(self, push, command, env=None, hz=FACE_HZ, clock=time.monotonic, poll_s=1.0):
        """push(text): sends a line to the laptop. command: the argument list that starts facewatch (e.g. ["on", "-p", "10", binary, model])."""
        self.push, self.command, self.env = push, command, env
        self.period, self.clock, self.poll_s = 1.0 / hz, clock, poll_s
        self.proc = None
        self.starts = []                           # when it was started, newest last
        self._cv = threading.Condition()
        self._frame = None
        self._last = -1e9
        self.frames_sent = 0
        self.max_crashes, self.pause_s = MAX_CRASHES, PAUSE_S

    def start(self):
        threading.Thread(target=self._supervise, daemon=True).start()
        threading.Thread(target=self._write, daemon=True).start()

    def submit(self, raw):
        """Offer the newest camera frame. Takes it at most FACE_HZ times a second; never waits."""
        now = self.clock()
        if now - self._last < self.period or self.proc is None:
            return
        self._last = now
        with self._cv:
            self._frame = raw
            self._cv.notify_all()

    def _write(self):
        while True:
            with self._cv:
                self._cv.wait_for(lambda: self._frame is not None)
                raw, self._frame = self._frame, None
            proc = self.proc
            if proc is None or proc.poll() is not None:
                continue
            try:
                proc.stdin.write(raw)
                proc.stdin.flush()
                self.frames_sent += 1
            except (OSError, ValueError):
                pass                               # it died: the supervisor starts a new one

    def _read(self, proc):
        for line in iter(proc.stdout.readline, b""):
            text = line.decode(errors="replace").strip()
            if text.startswith("FACE "):
                self.push(text)

    def _crash_loop(self, now):
        recent = [t for t in self.starts if now - t < CRASH_WINDOW_S]
        return len(recent) >= self.max_crashes

    def _supervise(self):
        while True:
            if self.proc is None or self.proc.poll() is not None:
                now = self.clock()
                if self._crash_loop(now):
                    self.proc = None
                    time.sleep(self.pause_s)
                    self.starts = []
                    continue
                try:
                    self.proc = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                                 env=self.env, bufsize=0)
                    self.starts.append(now)
                    threading.Thread(target=self._read, args=(self.proc,), daemon=True).start()
                except OSError:
                    self.proc = None
                    self.starts.append(now)
            time.sleep(self.poll_s)

    def stop(self):
        proc, self.proc = self.proc, None
        if proc is not None:
            try:
                proc.kill()
            except OSError:
                pass


def build(push, root=None):
    """The FaceWatch for this Pi if facewatch and its model are installed under ~/ai, else None."""
    root = root or os.path.expanduser("~/ai")
    binary = os.path.join(root, "fwbuild", "facewatch")
    model = os.path.join(root, "models", "blaze_face_short_range.tflite")
    if not (os.path.exists(binary) and os.path.exists(model)):
        return None
    env = dict(os.environ)
    libs = library_dirs(os.path.join(root, "build")) + library_dirs(os.path.join(root, "fwbuild"))
    env["LD_LIBRARY_PATH"] = ":".join(libs + ([env["LD_LIBRARY_PATH"]] if env.get("LD_LIBRARY_PATH") else []))
    return FaceWatch(push, ["on", "-p", "10", binary, model], env=env)
