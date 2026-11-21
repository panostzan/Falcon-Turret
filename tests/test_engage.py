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
    print("engagement tests passed")


if __name__ == "__main__":
    test()
