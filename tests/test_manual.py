"""Manual mode logic: slow steps, limits, and the platform/roof rules. No hardware: a fake gimbal."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "laptop"))
import manual as M   # noqa: E402

checks = 0


def check(cond, msg):
    global checks
    assert cond, msg
    checks += 1


class FakeGimbal:
    def __init__(self):
        self.moves = []

    def move_to(self, **kw):
        self.moves.append(kw)


def make():
    g = FakeGimbal()
    return g, M.ManualControl(g, (0, 45))


def settle(c, limit=500):
    for _ in range(limit):
        if not c.update():
            return
    raise AssertionError("never settled")


# the assumed rest position, and nothing is sent until a slider moves
g, c = make()
check(c.sent == {"pan": 90, "tilt": 22, "lift": 0, "roof": 140}, c.sent)
check(c.update() is False and g.moves == [], "nothing is sent until a slider moves")
check(c.ranges == {"pan": (0, 180), "tilt": (0, 45), "lift": (0, 150), "roof": (20, 140)}, c.ranges)

# a big drag is walked there in small steps, never jumped
check(c.set_target("pan", 150) is None, "pan is free to move")
settle(c)
steps = [m["pan"] for m in g.moves]
check(steps[0] == 90 + M.MAX_STEP and steps[-1] == 150 and all(b - a <= M.MAX_STEP for a, b in zip(steps, steps[1:])), "pan walks a few degrees at a time, never a jump")
check(c.sent["pan"] == 150 and c.settled, "and arrives")

# targets are clamped to each part's range
c.set_target("pan", 999); check(c.target["pan"] == 180, "pan stops at 180")
c.set_target("tilt", 999); check(c.target["tilt"] == 45, "tilt stops at the end of its window (45)")
c.set_target("tilt", -5); check(c.target["tilt"] == 0, "tilt stops at 0")
g, c = make()
c.set_target("roof", 0); check(c.target["roof"] == 20, "the roof can never be asked to open past 20")
c.set_target("roof", 500); check(c.target["roof"] == 20 or c.target["roof"] == 140, "nor past 140")

# the platform cannot rise while the roof is closed
g, c = make()
msg = c.set_target("lift", 120)
check(msg and "roof" in msg.lower() and c.target["lift"] <= M.LIFT_LOW_MAX, f"refused: {msg}")
settle(c)
check(not any("lift" in m and m["lift"] > M.LIFT_LOW_MAX for m in g.moves), "the platform never rose with the roof shut")
check(c.set_target("lift", 8) is None and c.target["lift"] == 8, "a small lift (still 'down') is allowed")

# open the roof (slowly), and only then may the platform rise
g, c = make()
check(c.set_target("roof", 20) is None, "opening the roof is allowed")
for _ in range(3):
    c.update()
check(c.sent["roof"] == 140 - 3 * M.MAX_STEP, "each update opens it by one step")
check(c.set_target("lift", 150) is not None, "the roof is not open enough yet (still above 100): platform refused")
settle(c)
check(c.sent["roof"] == 20, "the roof reached 20")
check(c.set_target("lift", 150) is None, "now the platform may rise")
settle(c)
check(c.sent["lift"] == 150 and c.sent["roof"] == 20, "up")
vals = [m["lift"] for m in g.moves if "lift" in m]
check(max(b - a for a, b in zip(vals, vals[1:])) <= M.MAX_STEP, "the platform also rises in steps, not a jump")

# the roof cannot close onto a raised platform
msg = c.set_target("roof", 140)
check(msg and "platform" in msg.lower() and c.target["roof"] == 20, f"refused: {msg}")
check(c.set_target("roof", 95) is None, "a small change that keeps it open is fine")
settle(c)

# lower the platform, then the roof may close
c.set_target("lift", 0); settle(c)
check(c.set_target("roof", 140) is None, "platform down: the roof may close")
settle(c)
check(c.sent["roof"] == 140 and c.sent["lift"] == 0, "closed")

# the rules look at where parts really are: a roof target set while the platform is still coming down waits for it
g, c = make()
c.set_target("roof", 90); settle(c)
c.set_target("lift", 100); settle(c)
c.set_target("lift", 0)
c.target["roof"] = 140                                  # even a target forced past the rule cannot make the roof move early
log_before = len(g.moves)
c.update()
check(not any(m.get("aux", 0) > M.OPEN_ENOUGH for m in g.moves[log_before:]), "the roof holds while the platform is still up")
settle(c)
check(c.sent == {"pan": 90, "tilt": 22, "lift": 0, "roof": 140}, "and then closes once the platform is down")

# park: back to rest in a safe order, sliders locked meanwhile
g, c = make()
c.set_target("roof", 90); settle(c)
c.set_target("lift", 170); c.set_target("pan", 20); c.set_target("tilt", 40); settle(c)
g.moves.clear()
c.park()
check(c.locked and c.set_target("pan", 100) and "locked" in c.set_target("pan", 100), "sliders are locked while parking")
broke = 0
for _ in range(500):                                      # step by step: the rule must hold at every single step
    if not c.update():
        break
    if c.sent["roof"] > M.OPEN_ENOUGH and c.sent["lift"] > M.LIFT_LOW_MAX:
        broke += 1
check(broke == 0, "parking: the roof never closes past 'open enough' while the platform is still up")
check(c.sent == {"pan": 90, "tilt": 22, "lift": 0, "roof": 140} and c.settled, c.sent)
check(all(90 <= m["aux"] <= 140 for m in g.moves if "aux" in m), "roof moves stay inside 90-140")

# unknown part
check(c.set_target("barrel", 5) is not None, "an unknown part is refused")
info = c.info()
check(set(info) == {"sent", "target", "ranges", "locked", "settled"}, info)
print(f"ok: {checks} manual-mode checks passed")
