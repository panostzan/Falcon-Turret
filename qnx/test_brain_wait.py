"""Runs ON THE PI. Checks that the status that travels with a JPEG describes THAT picture (not the one before).
Compares the old behaviour (take whatever turretd has said so far) with the new one (wait up to 20 ms for this picture's
answer). Usage (from ~/turret_c, with ~/bin/pi_stream.py updated): python3 test_brain_wait.py frames.yuy2"""
import os
import socket
import sys
import time

sys.path.insert(0, os.path.expanduser("~/bin"))
import pi_stream as ps

data = open(sys.argv[1], "rb").read()
frames = [data[i:i + ps.FRAME] for i in range(0, len(data) - ps.FRAME + 1, ps.FRAME)]
brain = ps.Brain()
assert brain.ok, "turretd did not start"
ctl = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
for line in (b"preset teal", b"track 1"):
    ctl.sendto(line, ps.CONTROL)
time.sleep(0.5)


def seqs(lines):
    return [int(l.split()[1]) for l in lines if l.startswith("T ")]


count = [0]          # pictures submitted so far; turretd's own picture counter must line up with this
base = [None]


def run(wait, n=60, encode_ms=2):
    """How many pictures old is the answer that travels with each picture? 0 = describes this picture, 1 = the one before.
    The two counters are lined up ONCE, using a long wait that is certain to return this picture's own answer."""
    lags, waits = [], []
    for i in range(n):
        marker = brain.submit(frames[count[0] % len(frames)])
        if base[0] is None:
            s = seqs(brain.take(marker, 1.0))
            base[0] = s[-1] - count[0]
            count[0] += 1
            time.sleep(0.1)
            continue
        time.sleep(encode_ms / 1000)                  # stands in for the JPEG encode
        t0 = time.time()
        said = brain.take(marker, wait) if wait else brain.take()
        waits.append((time.time() - t0) * 1000)
        s = seqs(said)
        if s:
            lags.append(count[0] - (s[-1] - base[0]))
        count[0] += 1
        time.sleep(0.066 - encode_ms / 1000)
    return (sum(lags) / len(lags) if lags else float("nan")), len(lags), n, (sum(waits) / len(waits) if waits else 0.0), (max(waits) if waits else 0.0)


for _ in range(4):                                     # warm-up: turretd has to have said something recently
    brain.submit(frames[count[0] % len(frames)])
    count[0] += 1
    time.sleep(0.1)
run(0.02, n=1)                                         # line the counters up first
for ms in (2, 10, 20):
    old = run(0.0, encode_ms=ms)
    new = run(0.02, encode_ms=ms)
    print(f"encode {ms:2d} ms | old: answer is {old[0]:.2f} pictures old | new: {new[0]:.2f} pictures old, waited {new[3]:.1f} ms avg, {new[4]:.1f} ms worst")

# a dead turretd must not slow the stream: no waiting once it has gone quiet
brain.proc.kill()
time.sleep(0.8)
t0 = time.time()
marker = brain.submit(frames[0])
brain.take(marker, 0.02)
dead = (time.time() - t0) * 1000
print(f"turretd killed: take() returned in {dead:.1f} ms (must be well under 20)")
assert dead < 15
brain.close()
sys.stdout.flush()
os._exit(0)
