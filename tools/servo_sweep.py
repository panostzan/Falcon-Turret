"""Sweeps each servo across its range and back, a few times, slowly. A simple way to see that both servos move.

    python tools/servo_sweep.py                    pan 0-180 and tilt 0-45, 3 round trips each (pan first)
    python tools/servo_sweep.py --axis tilt        only one axis (pan, tilt or both)
    python tools/servo_sweep.py --tilt 5,40        a different tilt range (0-180 is the most the servo takes)
    python tools/servo_sweep.py --cycles 1 --step 1 --delay 0.08     gentler: 1 degree steps, slower

Notes
  * Opening the serial port resets the ESP32, so the servos jump to their start position (pan 90, tilt 22) first.
  * The dashboard server must not be tracking at the same time (it would be holding the port).
  * The default tilt range is 0-45. A servo pushed against a mechanical stop heats up fast, so widen it only if the
    bracket really allows it.
  * Ctrl+C stops the sweep where it is.
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "laptop"))
from gimbal import CENTER, PAN_RANGE, TILT_RANGE   # noqa: E402

DEFAULT_TILT = (0, 45)


def legs(lo, hi, step):
    """Angles from lo up to hi, in steps of at most `step` degrees (always ending exactly on hi)."""
    angles, a = [], lo
    while a < hi:
        angles.append(a)
        a += step
    angles.append(hi)
    return angles


def sweep_axis(gimbal, axis, lo, hi, cycles, step, delay, sleep=time.sleep, say=print):
    """Go from the current angle to lo, then lo -> hi -> lo, `cycles` times, then to the middle. Returns the angles sent."""
    sent = []

    def go(angle):
        gimbal.move_to(**{axis: angle})
        sent.append(angle)
        sleep(delay)

    start = gimbal.pan if axis == "pan" else gimbal.tilt
    say(f"{axis}: moving gently to {lo} ...")
    cur = start
    while cur != lo:                                     # a slow ramp from wherever it is to the start of the range
        cur = min(cur + step, lo) if lo > cur else max(cur - step, lo)
        go(cur)
    for n in range(1, cycles + 1):
        say(f"{axis}: round trip {n} of {cycles}: {lo} -> {hi} -> {lo}")
        for a in legs(lo, hi, step)[1:]:
            go(a)
        for a in reversed(legs(lo, hi, step)[:-1]):
            go(a)
    mid = (lo + hi) // 2
    say(f"{axis}: back to the middle ({mid})")
    cur = lo
    while cur != mid:
        cur = min(cur + step, mid)
        go(cur)
    return sent


def main(argv=None, gimbal=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", default=os.environ.get("GIMBAL", "COM7"))
    p.add_argument("--axis", choices=("pan", "tilt", "both"), default="both")
    p.add_argument("--cycles", type=int, default=3)
    p.add_argument("--step", type=int, default=2, help="degrees per move (default 2)")
    p.add_argument("--delay", type=float, default=0.05, help="seconds between moves (default 0.05)")
    p.add_argument("--tilt", default="0,45", help="tilt range LO,HI in degrees (default 0,45; the servo takes 0-180)")
    a = p.parse_args(argv)
    if not (1 <= a.cycles <= 20 and 1 <= a.step <= 10 and 0.01 <= a.delay <= 1.0):
        sys.exit("cycles 1-20, step 1-10 degrees, delay 0.01-1 s")
    try:
        tilt = tuple(int(v) for v in a.tilt.split(","))
        assert len(tilt) == 2 and TILT_RANGE[0] <= tilt[0] < tilt[1] <= TILT_RANGE[1]
    except (ValueError, AssertionError):
        sys.exit(f"--tilt must be LO,HI inside {TILT_RANGE[0]}-{TILT_RANGE[1]}, for example 0,45")
    if gimbal is None:
        from gimbal import Gimbal
        print(f"opening {a.port} (this resets the ESP32; the servos jump to {CENTER[0]}/{CENTER[1]} first) ...")
        gimbal = Gimbal(a.port, tilt_limits=TILT_RANGE)     # the --tilt range is checked by this tool, so no other limit
        print("start position:", gimbal.status())
    try:
        if a.axis in ("pan", "both"):
            sweep_axis(gimbal, "pan", PAN_RANGE[0], PAN_RANGE[1], a.cycles, a.step, a.delay)
        if a.axis in ("tilt", "both"):
            sweep_axis(gimbal, "tilt", tilt[0], tilt[1], a.cycles, a.step, a.delay)
        print("done. final position:", gimbal.status())
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        if hasattr(gimbal, "close"):
            gimbal.close()


if __name__ == "__main__":
    main()
