"""Runs ON THE PI (QNX, Python 3, no numpy). Starts turretd, feeds it raw YUY2 pictures from a file at ~15 per second,
sends it control lines over UDP, and checks what it does. Usage: python3 test_turretd.py frames.yuy2 [w h]"""
import os
import socket
import subprocess
import sys
import threading
import time

frames_file = sys.argv[1]
W, H = (int(sys.argv[2]), int(sys.argv[3])) if len(sys.argv) > 3 else (640, 480)
size = W * H * 2
data = open(frames_file, "rb").read()
frames = [data[i:i + size] for i in range(0, len(data) - size + 1, size)]
PORT = 5105

proc = subprocess.Popen(["./turretd", str(W), str(H), str(PORT)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, bufsize=0)
ctl = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
out_lines, lock = [], threading.Lock()


def reader():
    buf = b""
    while True:
        chunk = os.read(proc.stdout.fileno(), 4096)
        if not chunk:
            return
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            with lock:
                out_lines.append((time.time(), line.decode()))


threading.Thread(target=reader, daemon=True).start()


def send(text):
    ctl.sendto(text.encode(), ("127.0.0.1", PORT))


run = {"stop": False, "hb": True, "n": 0}


def feeder():
    i = 0
    while not run["stop"]:
        try:
            proc.stdin.write(frames[i % len(frames)])
        except (BrokenPipeError, OSError):
            return
        i += 1
        run["n"] = i
        time.sleep(1 / 15)


def heartbeats():
    while not run["stop"]:
        if run["hb"]:
            send("hb")
        time.sleep(0.2)


threading.Thread(target=feeder, daemon=True).start()
threading.Thread(target=heartbeats, daemon=True).start()


def snapshot():
    with lock:
        ts = [l.split() for t, l in out_lines if l.startswith("T ")]
        ev = [l for t, l in out_lines if l.startswith("E ")]
    return ts, ev


def wait_for(cond, timeout):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.1)
    return False


results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(("PASS " if ok else "FAIL ") + name + (" - " + detail if detail else ""), flush=True)


time.sleep(1.0)
ts, ev = snapshot()
check("running with no target set: frames processed, nothing found, state IDLE",
      len(ts) > 5 and all(t[3] == "0" for t in ts[-5:]) and ts[-1][9] == "IDLE", f"{len(ts)} pictures")

send("preset teal"); send("track 1"); send("aim 1 10")
ok = wait_for(lambda: snapshot()[0] and snapshot()[0][-1][3] == "1", 5)
ts, ev = snapshot()
check("finds the teal box in the camera format (YUY2)", ok, f"cx={ts[-1][4]} cy={ts[-1][5]} conf={ts[-1][8]}")
ok = wait_for(lambda: snapshot()[0][-1][9] in ("LOCK", "COOLDOWN"), 6)
ts, ev = snapshot()
check("reaches LOCK (settled aim, confident target)", ok, ts[-1][9])
time.sleep(1.5)
ts, ev = snapshot()
check("UNARMED: locked for over a second and fired nothing", ts[-1][10] == "0" and ts[-1][11] == "0" and not any("FIRE" in e for e in ev))

send("arm 1")
ok = wait_for(lambda: any("FIRE #1" in e for e in snapshot()[1]), 5)
ts, ev = snapshot()
check("ARMED + locked + confident: fires", ok, "; ".join(e[:40] for e in ev[-3:]))

send("min_conf 100"); send("arm 0"); send("arm 1")
time.sleep(0.3)
before = len([e for e in snapshot()[1] if "FIRE" in e])
time.sleep(3.0)
after = len([e for e in snapshot()[1] if "FIRE" in e])
check("confidence below the required level: no shot", after == before, f"fires before {before} after {after}")
send("min_conf 80")
ok = wait_for(lambda: len([e for e in snapshot()[1] if "FIRE" in e]) > after, 6)
check("required level back to 80%: fires again", ok)

run["hb"] = False                                  # the laptop goes away
ok = wait_for(lambda: any("WATCHDOG" in e for e in snapshot()[1]), 4)
time.sleep(0.5)
ts, ev = snapshot()
check("WATCHDOG: heartbeats stop, QNX disarms by itself", ok and ts[-1][10] == "0" and ts[-1][12] == "0", f"armed={ts[-1][10]} link={ts[-1][12]}")
f1 = len([e for e in ev if "FIRE" in e])
time.sleep(3.0)
f2 = len([e for e in snapshot()[1] if "FIRE" in e])
check("after the watchdog: no more shots even with a locked target", f2 == f1)
run["hb"] = True
time.sleep(1.0)
ts, ev = snapshot()
check("heartbeats return: link restored but STILL disarmed", ts[-1][12] == "1" and ts[-1][10] == "0" and any("restored" in e for e in ev))

procs = [int(t[13]) for t in snapshot()[0]]
procs.sort()
print(f"\nprocessing time per picture on the Pi: median {procs[len(procs)//2]} us, 95th percentile {procs[int(len(procs)*0.95)]} us, max {procs[-1]} us ({len(procs)} pictures)")
run["stop"] = True
proc.stdin.close()
time.sleep(0.3)
proc.kill()
print("\nALL PASSED" if all(results) else "\nSOME FAILED")
sys.exit(0 if all(results) else 1)
