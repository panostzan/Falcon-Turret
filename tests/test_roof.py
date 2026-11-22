"""Roof state machine: order of the servos, waking, going back to sleep, hibernation. No hardware: a fake clock and fake servos."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "laptop"))
import roof as R   # noqa: E402

R.ROOF_MOVE_S = 0.45               # the older checks below use a quick roof so their timings stay short; the gentle-roof checks set the real one
R.ROOF_OPEN_S = None      # these timings assume one roof speed for both directions


def merged(log):
    """The log boiled down to one entry per servo, in the order each first moved, with the angle it ended on."""
    final = {}
    for _, cmd in log:
        for axis, v in cmd.items():
            final[axis] = v
    return [{a: v} for a, v in final.items()]


def first_times(log):
    t = {}
    for at, cmd in log:
        for axis in cmd:
            t.setdefault(axis, at)
    return t


checks = 0


def check(cond, msg):
    global checks
    assert cond, msg
    checks += 1


class Rig:
    """A roof with a log of every servo command (time, command), driven by a clock we advance by hand."""

    def __init__(self, hibernating=False):
        self.t, self.log, self.closes = 100.0, [], 0
        self.roof = R.Roof(self._move, self._closed, hibernating=hibernating)

    def _move(self, **kw):
        self.log.append((round(self.t, 3), kw))

    def _closed(self):
        self.closes += 1

    def run(self, seconds, seen=None, step=0.02):
        """Advance time; `seen` says whether the target is in view during this stretch."""
        end = self.t + seconds
        while self.t < end - 1e-9:
            self.t += step
            if seen is not None:
                self.roof.observe(self.t, seen)
            self.roof.update(self.t)

    def wake(self):
        self.roof.start(self.t)
        self.run(1.0)
        self.log.clear()
        self.run(0.5, seen=True)
        self.run(1.2, seen=True)


# start: tracking begins, position unknown -> platform down first, then the roof closes
r = Rig()
r.roof.start(r.t)
check(r.roof.state == R.CLOSING and r.closes == 1, "start begins a close and tells the app (disarm, park)")
r.run(1.0)
check(merged(r.log) == [{"lift": R.LIFT_DOWN}, {"aux": R.ROOF_CLOSED}], r.log)
ft = first_times(r.log)
check(ft["aux"] - ft["lift"] >= R.STAGGER_S - 0.03, "closing: the roof starts a moment AFTER the platform")
check(r.roof.state == R.DORMANT and r.roof.closed, "after the close it is dormant")

# a target that is not seen long enough does nothing
r = Rig(); r.roof.start(r.t); r.run(1.0); r.log.clear()
r.run(0.2, seen=True); r.run(2.0, seen=False)
check(r.roof.state == R.DORMANT and not r.log, "a 0.2 s glimpse does not open the roof")

# a target seen for WAKE_S opens it: roof first, platform after, simultaneous allowed, ACTIVE only when both are done
r = Rig(); r.roof.start(r.t); r.run(1.0); r.log.clear()
r.run(0.5, seen=True)
check(r.roof.state in (R.OPENING, R.ACTIVE), "seen for 0.3 s wakes it")
r.run(1.5, seen=True)
check(merged(r.log) == [{"aux": R.ROOF_OPEN}, {"lift": R.LIFT_UP}], r.log)
ft = first_times(r.log)
check(0.15 <= ft["lift"] - ft["aux"] <= 0.32, "opening: the platform starts 0.2-0.3 s AFTER the roof")
check(r.roof.state == R.ACTIVE and r.roof.can_aim, "open and up: active")
check(not Rig().roof.can_aim, "a fresh roof cannot aim")

# while opening the roof cannot aim yet
r = Rig(); r.roof.start(r.t); r.run(1.0)
r.run(0.35, seen=True)
check(r.roof.state == R.OPENING and not r.roof.can_aim, "no aiming while it is still opening")

# one missed frame does not break the run
r = Rig(); r.roof.start(r.t); r.run(1.0); r.log.clear()
r.run(0.2, seen=True); r.run(0.1, seen=False); r.run(0.2, seen=True)
check(r.roof.state != R.DORMANT, "a single missed frame (0.1 s) does not reset the wake-up run")

# staying open while the target is around, closing 10 s after it is gone
r = Rig(); r.wake()
r.run(30, seen=True)
check(r.roof.state == R.ACTIVE, "stays open while the target is in view")
r.run(9.0, seen=False)
check(r.roof.state == R.ACTIVE, "still open 9 s after the target left")
info = r.roof.info(r.t)
check(info["state"] == R.ACTIVE and 0 <= info["sleep_in"] <= 1.5, info)
r.log.clear()
r.run(1.3, seen=False)
check(r.roof.state == R.CLOSING or r.roof.state == R.DORMANT, "closes after 10 s without the target")
r.run(1.5, seen=False)
check(r.roof.state == R.DORMANT and r.roof.closed, "and ends dormant")
check(merged(r.log) == [{"lift": R.LIFT_DOWN}, {"aux": R.ROOF_CLOSED}], "closing order: platform down first, then the roof")
check(r.closes == 2, "the app is told at each close (start and this one)")

# target comes back during the close: it finishes closing, then wakes again
r = Rig(); r.wake()
r.run(10.1, seen=False)
r.run(0.3, seen=True)
check(r.roof.state in (R.CLOSING, R.DORMANT), "a close in progress is not reversed")
r.run(2.0, seen=True)
check(r.roof.state in (R.OPENING, R.ACTIVE), "once closed, a target in view opens it again")

# hibernate from dormant: nothing happens, whatever is in view
r = Rig(); r.roof.start(r.t); r.run(1.0); r.log.clear()
r.roof.set_hibernate(True, r.t)
check(r.roof.state == R.HIBERNATE and r.roof.closed, "hibernate from dormant: closed, resting")
r.run(5.0, seen=True)
check(r.roof.state == R.HIBERNATE and not r.log and not r.roof.can_aim, "hibernating: a target in view does nothing at all")
r.roof.set_hibernate(False, r.t)
check(r.roof.state == R.DORMANT, "switching back gives dormant")
r.run(0.1, seen=True)
check(r.roof.state == R.DORMANT, "a target already in view must be seen afresh (no instant opening)")
r.run(1.5, seen=True)
check(r.roof.state in (R.OPENING, R.ACTIVE), "and then it wakes normally")

# hibernate while active: closes in order first, then rests
r = Rig(); r.wake(); r.log.clear(); before = r.closes
r.roof.set_hibernate(True, r.t)
check(r.roof.state == R.CLOSING and r.closes == before + 1 and not r.roof.can_aim, "hibernate while active starts a close (and disarms)")
r.run(1.5, seen=True)
check(r.roof.state == R.HIBERNATE and r.roof.closed, "it ends hibernating, closed, even with the target in view")
check(merged(r.log) == [{"lift": R.LIFT_DOWN}, {"aux": R.ROOF_CLOSED}], r.log)

# hibernate while opening: the opening is cancelled, platform is not raised
r = Rig(); r.roof.start(r.t); r.run(1.0); r.log.clear()
r.run(0.35, seen=True)
r.log.clear()
r.roof.set_hibernate(True, r.t)
r.run(1.5, seen=True)
check(not any("lift" in c and c["lift"] == R.LIFT_UP for _, c in r.log), "hibernate while opening never raises the platform")
check(r.roof.state == R.HIBERNATE, "and ends hibernating")

# hibernating from the start (the button was pressed before tracking began)
r = Rig(hibernating=True); r.roof.start(r.t); r.run(1.0); r.run(3.0, seen=True)
check(r.roof.state == R.HIBERNATE and not r.roof.can_aim, "tracking started while hibernating: closed, nothing wakes it")

# shutdown (tracking switched off) closes an active turret, and is a no-op when already closed
r = Rig(); r.wake(); r.log.clear()
r.roof.shutdown(r.t); r.run(1.5)
check(r.roof.closed and merged(r.log) == [{"lift": R.LIFT_DOWN}, {"aux": R.ROOF_CLOSED}], "shutdown closes up in order")
r.log.clear(); r.roof.shutdown(r.t); r.run(1.0)
check(not r.log, "shutdown when already closed sends nothing")

# a failed command is retried, not lost
r = Rig(); calls = {"n": 0}
def flaky(**kw):
    calls["n"] += 1
    if calls["n"] == 1:
        raise OSError("port busy")
    r.log.append((r.t, kw))
r.roof.move = flaky
r.roof.start(r.t)
try:
    r.roof.update(r.t + 0.01)
except OSError:
    pass
r.run(1.0)
check(merged(r.log) == [{"lift": R.LIFT_DOWN}, {"aux": R.ROOF_CLOSED}], "a failed send is repeated, in order")

# the constants are the ones the hardware needs
check((R.ROOF_CLOSED, R.ROOF_OPEN, R.LIFT_DOWN, R.LIFT_UP) == (140, 20, 0, 150), "roof 140 closed / 20 open; platform 0 down / 150 up (its highest point)")
check(R.SLEEP_AFTER_S == 10.0, "10 s without the target")

# ---- the gentle roof: walked to its angle, not thrown there ----
R.ROOF_MOVE_S = 0.9
r = Rig(); r.roof.start(r.t); r.run(1.6); r.log.clear()
check(r.roof._aux == R.ROOF_CLOSED, "after the first (unknown-start) close the roof's angle is known")
r.run(0.5, seen=True); r.run(2.0, seen=True)
aux = [(t, c["aux"]) for t, c in r.log if "aux" in c]
vals = [v for _, v in aux]
check(vals[0] < 140 and vals[-1] == R.ROOF_OPEN == 20 and all(a >= b for a, b in zip(vals, vals[1:])), "opening: the roof steps steadily from 140 down to 60")
check(max(a - b for a, b in zip(vals, vals[1:])) <= 4, "no step bigger than 4 degrees (about 160 degrees/s at the fastest): not a jump")
check(0.7 <= aux[-1][0] - aux[0][0] <= 1.1, f"the roof takes about 0.9 s, got {aux[-1][0] - aux[0][0]:.2f}")
slow_start, mid = vals[0] - vals[1] if len(vals) > 1 else 0, max(a - b for a, b in zip(vals, vals[1:]))
check(slow_start <= 1 and mid >= 2, "soft start: the first steps are small, the middle is quicker")
lift_t = [t for t, c in r.log if c.get("lift") == R.LIFT_UP][0]
check(lift_t - aux[0][0] >= 0.15, "the platform still starts after the roof has begun to open")
check(r.roof.state == R.ACTIVE and not r.roof.closed, "active only after the roof has arrived and the platform is up")

# closing walks the roof back to 140 and only then is it dormant
r.log.clear(); r.run(11.0, seen=False); r.run(1.6, seen=False)
aux = [c["aux"] for _, c in r.log if "aux" in c]
check(aux[-1] == 140 and all(b >= a for a, b in zip(aux, aux[1:])) and max(b - a for a, b in zip(aux, aux[1:])) <= 4, "closing: 60 up to 140 in small steps")
check(r.roof.state == R.DORMANT and r.roof.closed, "dormant only once the roof has arrived")

# interrupted mid-way (Hibernate while opening): it turns round from where it is, with no jump
r = Rig(); r.roof.start(r.t); r.run(1.6); r.log.clear()
r.run(0.5, seen=True); r.run(0.45, seen=True)
here = r.roof._aux
check(R.ROOF_OPEN < here < R.ROOF_CLOSED, f"half way through the opening ({here})")
r.roof.set_hibernate(True, r.t); r.run(2.0, seen=True)
aux = [c["aux"] for _, c in r.log if "aux" in c]
check(max(abs(b - a) for a, b in zip(aux, aux[1:])) <= 5, "turning round mid-way never jumps")
check(aux[-1] == 140 and r.roof.state == R.HIBERNATE, "and it ends closed, hibernating")

# a slower setting is slower
R.ROOF_MOVE_S = 1.8
r = Rig(); r.roof.start(r.t); r.run(3.0); r.log.clear()
r.run(0.5, seen=True); r.run(3.0, seen=True)
aux = [t for t, c in r.log if "aux" in c]
check(1.5 <= aux[-1] - aux[0] <= 2.0, "the time can be set: 1.8 s gives about 1.8 s")
R.ROOF_MOVE_S = 1.4
check(R.ROOF_MOVE_S == 1.4 and R.ROOF_OPEN == 20, "defaults: 1.4 s, opens to 20")
R.ROOF_OPEN_S = float(__import__("os").environ.get("TURRET_ROOF_OPEN_SECONDS", 0.4))   # back to the shipped default
check(R.ROOF_MOVE_S == 1.4 and R.ROOF_OPEN_S == 0.4, "defaults: closes over 1.4 s, opens over 0.4 s")
R.ROOF_OPEN_S = 0.4
check(R._open_s() == 0.4, "opening uses its own quick time")
R.ROOF_OPEN_S = None
check(R._open_s() == R.ROOF_MOVE_S, "no separate opening time: same as closing")

print(f"ok: {checks} roof checks passed")
