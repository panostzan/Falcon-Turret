"""Runs ON THE PI. Measures how many pictures per second the C270 really produces, with none of our streaming code involved: it starts the
same QNX recorder our streamer uses, watches the recording file grow, and counts pictures and the gaps between them.

    python3 camera_rate_test.py [recorder_fps] [seconds] [viewfinder_fps]        e.g.  python3 camera_rate_test.py 30 10 30

Run it with the dashboard stopped (only one program can use the camera). It deletes its recording when done and changes nothing else.
"""
import glob
import os
import subprocess
import sys
import time

FPS = sys.argv[1] if len(sys.argv) > 1 else "30"
SECONDS = float(sys.argv[2]) if len(sys.argv) > 2 else 10.0
VIEWFINDER = sys.argv[3] if len(sys.argv) > 3 else None     # the recorder's -v option; unset = the recorder's own default
DIR = "/data/share/sensor"
HEADER, FRAME = 96, 640 * 480 * 2
STRIDE = FRAME + 8

subprocess.call(["slay", "-f", "camera_example4_record"], stderr=subprocess.DEVNULL)
for stale in glob.glob(DIR + "/VID_*.ucv"):
    os.remove(stale)
known = set(glob.glob(DIR + "/VID_*.ucv"))
cmd = ["camera_example4_record", "-u", "5", "-c", "none", "-f", "ucv", "-r", FPS] + (["-v", VIEWFINDER] if VIEWFINDER else [])
proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(2.0)
proc.stdin.write(b"\n")
proc.stdin.flush()
path = None
t0 = time.time()
while path is None and time.time() - t0 < 6:
    new = set(glob.glob(DIR + "/VID_*.ucv")) - known
    path = new.pop() if new else None
    time.sleep(0.05)
if path is None:
    print("the recorder produced no file: is the camera plugged in?")
    proc.kill()
    sys.exit(1)

stamps, count = [], 0
start = time.time()
while time.time() - start < SECONDS:
    n = max(0, (os.path.getsize(path) - HEADER) // STRIDE)
    now = time.time()
    while count < n:
        count += 1
        stamps.append(now)
    time.sleep(0.01)
proc.kill()
proc.wait()
try:
    os.remove(path)
except OSError:
    pass

gaps = sorted((b - a) * 1000 for a, b in zip(stamps, stamps[1:]))
if len(gaps) < 3:
    print(f"only {len(stamps)} pictures in {SECONDS:.0f} s: the camera is delivering almost nothing")
    sys.exit(1)
span = stamps[-1] - stamps[0]
pick = lambda q: gaps[min(len(gaps) - 1, int(len(gaps) * q))]
print(f"recorder asked for {FPS} fps" + (f", viewfinder {VIEWFINDER}" if VIEWFINDER else "") + f" | measured {(len(stamps) - 1) / span:.1f} pictures per second over {span:.1f} s ({len(stamps)} pictures)")
print(f"gap between pictures: median {pick(0.5):.0f} ms, 95th {pick(0.95):.0f} ms, worst {gaps[-1]:.0f} ms")
print(f"(for comparison: 30 fps would be 33 ms between pictures, 15 fps would be 67 ms)")
sys.stdout.flush()
os._exit(0)
