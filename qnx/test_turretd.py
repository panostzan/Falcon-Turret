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

# The laptop re-sends the same target about once a second to keep the Pi in step. That must NOT restart the confidence
# build-up (it used to: the confidence climbed to ~0.9 and was reset to 0 each time).
lows = []
for _ in range(14):
    send("preset teal")
    time.sleep(0.25)
    ts, ev = snapshot()
    lows.append(float(ts[-1][8]))
check("re-sending the SAME target (as the laptop does every second) keeps the confidence up", min(lows) >= 0.9, f"lowest {min(lows):.2f} over 3.5 s")
send("preset blue")                                    # a DIFFERENT target does start fresh (nothing blue in the picture)
time.sleep(0.5)
ts, ev = snapshot()
check("choosing a DIFFERENT target starts fresh", ts[-1][3] == "0" and float(ts[-1][8]) == 0.0)
send("preset teal")
ok = wait_for(lambda: snapshot()[0][-1][9] in ("LOCK", "COOLDOWN"), 8)
check("and back to teal: it finds the box and locks again", ok)

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

# manual fire: the operator's click
def shots():
    return len([e for e in snapshot()[1] if "FIRE #" in e])
send("autofire 0"); send("arm 0")
time.sleep(0.5)
n_disarmed = shots()
send("fire")
ok = wait_for(lambda: any("FIRE refused: not armed" in e for e in snapshot()[1]), 3)
check("MANUAL fire while disarmed is refused", ok and shots() == n_disarmed, f"shots {n_disarmed} -> {shots()}")
send("arm 1")
time.sleep(0.5)
n0 = shots()
time.sleep(3.0)
check("auto-fire off: armed and locked, still no automatic shot", shots() == n0, f"{n0} -> {shots()}")
send("fire")
ok = wait_for(lambda: any("(manual)" in e for e in snapshot()[1]), 3)
check("MANUAL fire while armed: fires once", ok and shots() == n0 + 1, f"{n0} -> {shots()}")
send("fire")
ok = wait_for(lambda: any("FIRE refused: cooldown" in e for e in snapshot()[1]), 3)
check("a second click inside the cooldown is refused", ok and shots() == n0 + 1)
send("autofire 1")

# the human gate (the AI says whether a human is in view)
send("autofire 0"); send("arm 1")
time.sleep(2.0)                                        # let the cooldown from the last shot pass
send("humangate 1"); send("human 1")
time.sleep(0.5)
ts, ev = snapshot()
check("status line carries the gate, the human flag and the AI's freshness", ts[-1][15] == "1" and ts[-1][16] == "1" and ts[-1][17] == "1", f"gate={ts[-1][15]} human={ts[-1][16]} ai_ok={ts[-1][17]}")
n_human = shots()
send("fire")
ok = wait_for(lambda: any("FIRE refused: human detected" in e for e in snapshot()[1]), 3)
check("a human in view: the click is refused", ok and shots() == n_human, f"shots {n_human} -> {shots()}")
send("faceinfo 14.5"); send("human 0")
time.sleep(0.5)
ts, ev = snapshot()
check("the AI's inference time reaches the status line", abs(float(ts[-1][18]) - 14.5) < 0.6, ts[-1][18])
send("fire")
ok = wait_for(lambda: shots() == n_human + 1, 3)
check("the human left: the click fires again", ok, f"shots {n_human} -> {shots()}")
time.sleep(2.0)                                        # cooldown passes; no new report from the AI for over 2 s
ts, ev = snapshot()
check("the AI goes silent: the status says so", ts[-1][17] == "0" and ts[-1][16] == "0", f"ai_ok={ts[-1][17]}")
n_stale = shots()
send("fire")
ok = wait_for(lambda: any("FIRE refused: face check offline" in e for e in snapshot()[1]), 3)
check("silent AI with the gate on: the click is refused (fail-safe)", ok and shots() == n_stale)
send("humangate 0")
time.sleep(0.3)
send("fire")
ok = wait_for(lambda: shots() == n_stale + 1, 3)
check("gate switched off: the AI is not consulted and the click fires", ok, f"shots {n_stale} -> {shots()}")
send("autofire 1")

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
