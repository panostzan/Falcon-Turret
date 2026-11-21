"""Manual servo tester: type angles and watch the gimbal.

Run in your own terminal:   python tools\\servo_test.py        (or  python tools\\servo_test.py udp:192.168.137.50)
Stop the dashboard first: only one program can use the ESP's USB port.

Angles are clamped to the safe ranges in laptop/limits.json (and the ESP firmware enforces them too). To find
where a servo REALLY needs to go outside those ranges, type `free` (lifts the tilt limit until the ESP reboots or
you type `normal`) and then move in small steps: `t+` / `t-`.
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "laptop"))
from gimbal import CENTER, PAN_RANGE, TILT_RANGE, Gimbal, GimbalError   # noqa: E402

HELP = f"""commands:
  <pan> <tilt>      go to both angles, e.g.  90 22
  p <n>   t <n>     go to one angle,         e.g.  t 30
  p+ [n]  p- [n]    step pan by n degrees (default 3)       t+ [n]  t- [n]   same for tilt
  c                 home ({CENTER[0]}, {CENTER[1]})              s   status (does not move anything)
  d                 release: servos go limp                 a   resume holding
  free              lift the tilt limit to 0-180 (careful: small steps!)     normal   back to the safe range
  limits            show the current ranges                 q   quit (servos stay where they are)"""


def handle(g, line, out=print):
    """Run one command. Returns False to quit. Never raises for bad input."""
    line = re.sub(r"[<>\[\]]", "", line.strip().lower())          # forgive <45> style brackets
    line = re.sub(r"^([pt][+-]?)(\d)", r" ", line)             # p90 -> p 90, t+3 -> t+ 3, p-10 -> p- 10
    words = line.split()
    if not words:
        return True
    cmd, args = words[0], words[1:]

    def show(result, asked=None):
        out(f"  ESP is now at: pan {result[0]}, tilt {result[1]}")
        if asked:
            for axis, want, got in zip(("pan", "tilt"), asked, result):
                if want is not None and want != got:
                    out(f"  (note: {axis} {want} was out of range, held at {got})")

    try:
        if cmd in ("q", "quit", "exit"):
            out("bye: the servos stay where they are.")
            return False
        if cmd in ("h", "help", "?"):
            out(HELP)
        elif cmd == "s":
            show(g.status())
        elif cmd == "c":
            show(g.center())
        elif cmd == "d":
            show(g.release())
            out("  released: the servos are limp. Any move re-attaches them.")
        elif cmd == "a":
            show(g.resume())
        elif cmd == "free":
            show(g.free())
            out("  TILT LIMIT LIFTED (0-180) until the ESP reboots or you type 'normal'. Move in small steps.")
        elif cmd == "normal":
            show(g.normal())
        elif cmd == "limits":
            out(f"  pan {g.pan_range[0]}..{g.pan_range[1]}   tilt {g.tilt_range[0]}..{g.tilt_range[1]}   home {CENTER}")
        elif len(words) == 2 and all(re.fullmatch(r"-?\d+(\.\d+)?", w) for w in words):
            pan, tilt = float(words[0]), float(words[1])
            show(g.move_to(pan=pan, tilt=tilt), (round(pan), round(tilt)))
        elif cmd in ("p", "t") and args and re.fullmatch(r"-?\d+(\.\d+)?", args[0]):
            v = float(args[0])
            res = g.move_to(pan=v) if cmd == "p" else g.move_to(tilt=v)
            show(res, (round(v), None) if cmd == "p" else (None, round(v)))
        elif cmd in ("p+", "p-", "t+", "t-"):
            n = float(args[0]) if args and re.fullmatch(r"\d+(\.\d+)?", args[0]) else 3.0
            n = n if cmd[1] == "+" else -n
            show(g.nudge(d_pan=n) if cmd[0] == "p" else g.nudge(d_tilt=n))
        else:
            out("  I didn't understand that. Type h for the command list.")
    except GimbalError as e:
        out(f"  the ESP refused that: {e}")
    return True


def main(port="COM7"):
    print(f"connecting to {port} ...")
    with Gimbal(port) as g:
        print(f"connected. The ESP is holding pan {g.status()[0]}, tilt {g.status()[1]} (nothing moved).")
        print(f"safe ranges: pan {PAN_RANGE[0]}..{PAN_RANGE[1]}, tilt {TILT_RANGE[0]}..{TILT_RANGE[1]}.  Type h for help.")
        while True:
            try:
                line = input("servo> ")
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not handle(g, line):
                break


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "COM7")
