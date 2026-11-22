"""Tests the engagement state machine and its safety interlocks. Run: python tests/test_engage.py"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "laptop"))
from engage import Engagement   # noqa: E402

DT = 1 / 15


def run(e, seconds, tracking=True, found=True, settled=True, speed=0.0, t0=0.0):
    """Feed constant inputs for `seconds`; returns (shots fired during the run, final time)."""
    t, fired = t0, 0
    while t < t0 + seconds:
        f, _ = e.update(t, tracking, found, settled, speed)
        fired += f
        t += DT
    return fired, t


def test():
    # not tracking: idle, never fires even when armed
    e = Engagement()
    e.arm(True, 0)
    assert run(e, 5, tracking=False)[0] == 0 and e.state == "IDLE"

    # tracking, target present, NOT armed: climbs to LOCK and stays there, never fires
    e = Engagement()
    fired, _ = run(e, 5)
    assert e.state == "LOCK" and fired == 0 and e.emitter.shots == []

    # armed: fires after the dwell time, then rests through the cooldown
    e = Engagement()
    e.arm(True, 0)
    fired, t = run(e, 1.5)
    assert fired == 1 and e.state == "COOLDOWN", (fired, e.state)
    fired, t = run(e, 10, t0=t)
    assert 3 <= fired <= 6, f"about one shot per (dwell + cooldown) = 2 s, got {fired} in 10 s"
    gaps = [b - a for a, b in zip(e.emitter.shots, e.emitter.shots[1:])]
    assert min(gaps) >= e.cooldown_s, f"shots closer than the cooldown: {gaps}"

    # shot cap since arming; re-arming resets it
    e = Engagement(max_shots=2)
    e.arm(True, 0)
    fired, t = run(e, 20)
    assert fired == 2 and e.shots == 2
    e.arm(False, t)
    e.arm(True, t)
    assert e.shots == 0 and run(e, 4, t0=t)[0] >= 1

    # an unsettled gimbal never locks, so never fires
    e = Engagement()
    e.arm(True, 0)
    assert run(e, 6, settled=False)[0] == 0 and e.state == "TRACK"

    # a target moving too fast breaks the lock and blocks fire
    e = Engagement()
    e.arm(True, 0)
    _, t = run(e, 1.0, speed=0.0)                    # locks (and may fire once at the end of the dwell)
    e2 = Engagement()
    e2.arm(True, 0)
    assert run(e2, 6, speed=500.0)[0] == 0 and e2.state != "LOCK"

    # losing the target for longer than lost_s drops to SEARCH and stops everything
    e = Engagement()
    e.arm(True, 0)
    _, t = run(e, 1.2)
    fired, t = run(e, 2.0, found=False, t0=t)
    assert e.state == "SEARCH" and fired == 0

    # a brief dropout (shorter than lost_s) breaks the lock but does not start a search
    e = Engagement()
    _, t = run(e, 1.5)
    assert e.state == "LOCK"
    _, t = run(e, 0.3, found=False, t0=t)
    assert e.state != "SEARCH"

    # disarming stops firing at once
    e = Engagement()
    e.arm(True, 0)
    _, t = run(e, 1.0)
    e.arm(False, t)
    assert run(e, 8, t0=t)[0] == 0

    # auto-scan: only while searching, only when enabled, always within the scan range
    e = Engagement()
    t, goals = 0.0, []
    for _ in range(300):
        _, g = e.update(t, True, False, False)
        goals.append(g)
        t += DT
    assert all(g is None for g in goals), "auto_scan is off by default"
    e.auto_scan = True
    goals = []
    for _ in range(300):
        _, g = e.update(t, True, False, False)
        goals.append(g)
        t += DT
    assert all(g is not None and 50 <= g[0] <= 130 and g[1] == 135 for g in goals)
    assert min(g[0] for g in goals) < 60 and max(g[0] for g in goals) > 120, "should sweep the whole range"
    _, g = e.update(t, True, True, True)             # a target appears: scanning stops
    assert g is None

    # manual fire (the operator's click)
    e = Engagement()
    assert e.fire(0.0) == "not armed" and e.shots == 0, "disarmed: refused"
    e.arm(True, 0.0)
    assert e.fire(0.0) == "tracking off" and e.shots == 0, "armed but tracking is off: refused"
    e.update(0.1, True, False, False)                        # tracking on, no target (SEARCH)
    assert e.fire(0.2) is None and e.shots == 1 and e.state == "COOLDOWN", "armed + tracking: fires, even without a lock"
    assert e.fire(0.3) == "cooldown" and e.shots == 1, "no second shot inside the cooldown"
    assert e.events[-1][1] == "FIRE refused: cooldown"
    run(e, 3, found=False, t0=0.4)                           # cooldown passes
    assert e.fire(5.0) is None and e.shots == 2
    e.arm(False, 6.0)
    assert e.fire(7.0) == "not armed", "disarming takes the trigger away at once"
    e = Engagement(max_shots=2)
    e.arm(True, 0.0)
    e.update(0.0, True, False, False)
    assert e.fire(0.1) is None and e.fire(3.0) is None and e.fire(6.0) == "shot limit" and e.shots == 2

    # auto-fire off: armed and locked, but it only shoots when told to
    e = Engagement()
    e.auto_fire = False
    e.arm(True, 0)
    fired, t = run(e, 5)
    assert fired == 0 and e.state == "LOCK" and e.shots == 0, "locked and armed, but no automatic shot"
    assert e.fire(t) is None and e.shots == 1, "the click still fires"

    # the human gate: off by default, never blocks; on, it blocks while a human is in view and when the AI is silent (fail-safe)
    e = Engagement()
    e.arm(True, 0.0)
    e.update(0.0, True, False, False)
    e.set_human(True, 0.1)
    assert e.human_block(0.2) is None, "gate off: a human in view blocks nothing"
    e.human_gate = True
    assert e.human_block(0.2) == "human detected"
    assert e.fire(0.3) == "human detected" and e.shots == 0, "armed + tracking, but a human is in view: refused"
    assert e.events[-1][1] == "FIRE refused: human detected"
    e.set_human(False, 0.5)
    assert e.events[-2][1] == "HUMAN cleared" or e.events[-1][1] == "HUMAN cleared"
    assert e.fire(0.6) is None and e.shots == 1, "the human left: the click fires"
    e.set_human(False, 1.0)
    assert e.human_block(3.5) == "face check offline", "no report for over 2 s: the gate blocks (fail-safe)"
    e = Engagement()
    e.human_gate = True
    e.arm(True, 0.0)
    e.update(0.0, True, False, False)
    assert e.fire(0.1) == "face check offline", "gate on and the AI has never reported: blocked"
    e = Engagement()
    e.auto_fire = True
    e.human_gate = True
    e.arm(True, 0)
    e.set_human(True, 0.0)
    t = 0.0
    fired = 0
    for _ in range(90):                                      # locked and armed, auto-fire on, but a human is in view the whole time
        e.set_human(True, t)
        f, _g = e.update(t, True, True, True)
        fired += f
        t += DT
    assert fired == 0 and e.state == "LOCK", "auto-fire respects the gate"
    for _ in range(60):
        e.set_human(False, t)
        f, _g = e.update(t, True, True, True)
        fired += f
        t += DT
    assert fired >= 1, "and fires again once the human leaves"
    print("engagement tests passed")


if __name__ == "__main__":
    test()
