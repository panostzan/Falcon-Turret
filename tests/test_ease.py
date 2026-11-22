"""Smooth motion: the easing step and the Aimer's glide, simulated (no hardware). Run: python tests/test_ease.py"""
import os
import sys

os.environ["TURRET_TILT_CALIBRATION"] = "none.json"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "laptop"))
import ease                                    # noqa: E402
from aim import Aimer                          # noqa: E402

fails = 0


def check(name, ok, detail=""):
    global fails
    print(("PASS " if ok else "FAIL ") + name + (" - " + detail if detail else ""))
    fails += 0 if ok else 1


def run(start, goal, seconds=3.0, dt=0.02, **kw):
    pos, vel, trace = float(start), 0.0, []
    for i in range(int(seconds / dt)):
        pos, vel = ease.step(pos, vel, float(goal), dt, **kw)
        trace.append((pos, vel))
    return trace


for start, goal in ((90, 170), (90, 10), (20, 22), (0, 180)):
    tr = run(start, goal)
    lo, hi = min(start, goal), max(start, goal)
    check(f"{start} -> {goal}: arrives exactly, never overshoots", tr[-1][0] == goal and all(lo - 1e-9 <= p <= hi + 1e-9 for p, _ in tr),
          f"final {tr[-1][0]}")

tr = run(90, 170)
arrive = next(i for i, (p, _) in enumerate(tr) if p == 170)          # the last step is the final snap onto the goal (under 0.25 deg)
acc = max(abs(tr[i][1] - tr[i - 1][1]) / 0.02 for i in range(1, arrive))
check("acceleration stays within the limit (that is the easing)", acc <= ease.DEFAULT_ACCEL * 1.01, f"max {acc:.0f} deg/s^2")
check("top speed stays within the limit", max(abs(v) for _, v in tr) <= ease.DEFAULT_VMAX * 1.001, f"max {max(abs(v) for _, v in tr):.0f} deg/s")
first = next(i for i, (p, _) in enumerate(tr) if p > 90.5)
check("starts gently: no jump on the first step", tr[0][0] - 90 < 2.0, f"first step {tr[0][0] - 90:.2f} deg")
done = next(i for i, (p, _) in enumerate(tr) if p == 170) * 0.02
check("an 80 degree move takes a sensible time", 0.3 < done < 1.0, f"{done:.2f} s")

# a target that sits still but whose measured position jitters (pixel noise): through the Aimer's smoothing and hysteresis the servo must stay put
class Fake:
    def __init__(self):
        self.pan, self.tilt, self.sent = 90, 22, []

    def move_to(self, pan=None, tilt=None):
        if pan is not None:
            self.pan = int(round(pan))
        if tilt is not None:
            self.tilt = int(round(tilt))
        self.sent.append((pan, tilt))
        return self.pan, self.tilt


g = Fake()
a = Aimer(g, ease={"vmax": 320.0, "accel": 1800.0})
a.update(0.9, 0.0)                      # target far right
check("update() alone does not move the servo (easing does it)", g.sent == [])
t, positions = 100.0, []
for _ in range(100):
    t += 0.02
    a.tick(t)
    positions.append(g.pan)
steps = [abs(b - a_) for a_, b in zip(positions, positions[1:])]
check("the servo glides to the goal in many small steps", g.pan > 150 and max(steps) <= 8, f"final {g.pan}, biggest step {max(steps)} deg")
check("and never goes past it", max(positions) <= round(90 + 0.9 * 90) + 1, f"max {max(positions)}")
n = len(g.sent)
for _ in range(20):
    t += 0.02
    a.tick(t)
check("at rest it sends nothing more", len(g.sent) == n)
a.update(0.9, 0.0)
a.reset()
t += 0.02
a.tick(t)
check("a lost target stops the glide", len(g.sent) == n)

import random
random.seed(3)
g3 = Fake()
c = Aimer(g3, ease={"vmax": 320.0, "accel": 1800.0})
t3 = 200.0
for i in range(400):                    # 400 pictures at 15 per second = 26 s; between pictures the glide runs at 50 Hz
    c.update(0.4 + random.uniform(-0.012, 0.012), 0.0 + random.uniform(-0.012, 0.012))    # about +-1 degree of jitter
    for _ in range(3):
        t3 += 0.0222
        c.tick(t3)
    if i == 150:
        mark = len(g3.sent)
check("pixel jitter on a still target: the servo settles and stays still", len(g3.sent) - mark <= 4 and abs(g3.pan - (90 + 0.4 * 90)) < 3,
      f"{len(g3.sent) - mark} commands in the last 17 s, at {g3.pan}")

g2 = Fake()
b = Aimer(g2)                           # default: no easing, jumps straight there
b.update(0.9, 0.0)
check("without easing the move is immediate (unchanged behaviour)", g2.pan > 150 and len(g2.sent) == 1)
print("\nALL PASSED" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
