"""The Pi-side supervisor of the face program, with a pretend facewatch. Run: python tests/test_aiproc.py"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "pi"))
import aiproc  # noqa: E402

FRAME = 640 * 480 * 2
fails = 0


def check(name, ok, detail=""):
    global fails
    print(("PASS " if ok else "FAIL ") + name + (" - " + detail if detail else ""))
    fails += 0 if ok else 1


tmp = tempfile.mkdtemp()
fake = os.path.join(tmp, "fake_facewatch.py")
open(fake, "w").write(
    "import sys\n"
    "n = 0\n"
    "while True:\n"
    "    data = sys.stdin.buffer.read(%d)\n"
    "    if len(data) < %d: break\n"
    "    n += 1\n"
    "    print('FACE 1 4.0 0.5 0.5 0.2 0.3 0.91 frame', n, flush=True)\n" % (FRAME, FRAME))

got = []
fw = aiproc.FaceWatch(got.append, [sys.executable, fake], hz=10.0, poll_s=0.05)
check("the AI is off until the Face check is switched on", fw.enabled is False)
fw.start()
t0 = time.time()
while fw.proc is None and time.time() - t0 < 5:
    time.sleep(0.05)
check("the supervisor starts the program", fw.proc is not None and fw.proc.poll() is None)

frame = bytes(FRAME)
for _ in range(20):                                          # switched off: offers are ignored, no frame reaches the AI
    fw.submit(frame)
    time.sleep(0.02)
check("switched off: no picture is handed to the AI", fw.frames_sent == 0, f"{fw.frames_sent} frames")
fw.enabled = True
sent = 0
t0 = time.time()
while time.time() - t0 < 1.0:                              # offer frames far faster than the limit
    fw.submit(frame)
    time.sleep(0.005)
time.sleep(0.5)
check("frames are handed over at the limit (10 per second), not every offer", 6 <= fw.frames_sent <= 14, f"{fw.frames_sent} frames in 1 s")
check("its FACE lines are forwarded to the laptop", len(got) >= 5 and all(g.startswith("FACE 1 4.0") for g in got), f"{len(got)} lines")

old = fw.proc
old.kill()
t0 = time.time()
while (fw.proc is None or fw.proc is old or fw.proc.poll() is not None) and time.time() - t0 < 5:
    time.sleep(0.05)
check("if the program dies it is started again", fw.proc is not None and fw.proc is not old and fw.proc.poll() is None)
fw.stop()
check("stop() ends it", fw.proc is None)

# a program that crashes at once must not be restarted in a tight loop
crash = aiproc.FaceWatch(lambda t: None, [sys.executable, "-c", "raise SystemExit(1)"], poll_s=0.05)
crash.max_crashes, crash.pause_s = 3, 0.5
crash.start()
time.sleep(1.2)
check("a crash loop is limited", len(crash.starts) <= 4, f"{len(crash.starts)} starts in 1.2 s")
crash.stop()

# without the program or model installed, build() returns None and nothing happens
check("build() is None when facewatch is not installed", aiproc.build(lambda t: None, root=tmp) is None)
print("\nALL PASSED" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
