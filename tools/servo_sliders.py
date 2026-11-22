"""Two sliders to move the pan and tilt servos by hand and see the angles being commanded.

    python tools/servo_sliders.py        (the dashboard server must not be tracking: it would be holding the port)

  Pan slider and Tilt slider   drag to move; the number is the angle being COMMANDED (0-180 each, no limits here)
  Arrow keys                   with a slider selected, nudge it by 1 degree (PageUp/PageDown: 10)
  Home                         pan 90, tilt 22 (the ESP's start position)
  Max speed                    the most a servo is told to move per update, so a sudden drag cannot slam it into a stop
  Mark tilt TOP / BOTTOM       with the camera at its highest / lowest SAFE position, press the button: it saves the tilt range
                               and direction for the dashboard (laptop/tilt_calibration.json), the same as tilt_watch.py
Every command is also written to captures/servo_sliders.log. Esc quits (the servos stay where they are).
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "laptop"))
from gimbal import CENTER, PAN_RANGE, TILT_RANGE   # noqa: E402
import tilt_calibration                            # noqa: E402

LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "captures", "servo_sliders.log")
UPDATE_MS = 30


class SliderControl:
    """The logic without a window: the sliders set TARGETS; every update moves each servo toward its target by at most
    `max_step` degrees and sends only what changed. Testable with a fake gimbal."""

    def __init__(self, gimbal, pan=CENTER[0], tilt=CENTER[1], max_step=6, log=None):
        self.g, self.max_step, self.log = gimbal, max_step, log
        self.target = {"pan": pan, "tilt": tilt}
        self.sent = {"pan": pan, "tilt": tilt}
        self.marks = {}

    @staticmethod
    def _clamp(axis, v):
        lo, hi = PAN_RANGE if axis == "pan" else TILT_RANGE
        return max(lo, min(hi, int(round(v))))

    def set_target(self, axis, value):
        self.target[axis] = self._clamp(axis, value)

    def nudge(self, axis, delta):
        self.set_target(axis, self.target[axis] + delta)

    def home(self):
        self.set_target("pan", CENTER[0])
        self.set_target("tilt", CENTER[1])

    def update(self):
        """One step: move toward the targets. Returns True if anything was sent."""
        moved = False
        for axis in ("pan", "tilt"):
            gap = self.target[axis] - self.sent[axis]
            if gap:
                step = max(-self.max_step, min(self.max_step, gap))
                new = self.sent[axis] + step
                self.g.move_to(**{axis: new})
                self.sent[axis] = new
                moved = True
                if self.log:
                    self.log(f"{time.strftime('%H:%M:%S')} commanded {axis} {new}")
        return moved

    def mark(self, which):
        self.marks[which] = self.sent["tilt"]
        if self.log:
            self.log(f"{time.strftime('%H:%M:%S')} MARK tilt {which} = {self.sent['tilt']}")
        if "top" in self.marks and "bottom" in self.marks:
            return tilt_calibration.from_marks(self.marks["top"], self.marks["bottom"])
        return None


def run_window(port):
    import tkinter as tk
    from gimbal import Gimbal

    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    logfile = open(LOG, "a", encoding="utf-8")

    def log(text):
        logfile.write(text + "\n")
        logfile.flush()

    root = tk.Tk()
    root.title("Servo sliders")
    root.configure(bg="#14170f")
    root.geometry("620x440")
    fg, dim, accent = "#dfe6cf", "#8b9777", "#b6e23a"
    status = tk.Label(root, text="opening the port (this resets the ESP)...", fg=fg, bg="#14170f")
    status.pack(pady=(10, 4))
    state = {"ctl": None, "labels": {}, "scales": {}}

    def make_slider(axis, rng):
        frame = tk.Frame(root, bg="#14170f")
        frame.pack(fill="x", padx=20, pady=6)
        tk.Label(frame, text=axis.upper(), width=6, fg=accent, bg="#14170f", font=("Consolas", 14, "bold")).pack(side="left")
        big = tk.Label(frame, text="--", width=5, fg=accent, bg="#14170f", font=("Consolas", 32, "bold"))
        scale = tk.Scale(frame, from_=rng[0], to=rng[1], orient="horizontal", length=380, showvalue=False,
                         bg="#1f2418", fg=fg, troughcolor="#0a0d07", highlightthickness=0,
                         command=lambda v, a=axis: state["ctl"] and state["ctl"].set_target(a, float(v)))
        scale.pack(side="left", padx=8)
        big.pack(side="left")
        state["labels"][axis], state["scales"][axis] = big, scale

    make_slider("pan", PAN_RANGE)
    make_slider("tilt", TILT_RANGE)
    speed_row = tk.Frame(root, bg="#14170f")
    speed_row.pack(pady=4)
    tk.Label(speed_row, text="Max speed (degrees per update)", fg=dim, bg="#14170f").pack(side="left")
    speed = tk.Scale(speed_row, from_=1, to=20, orient="horizontal", length=160, bg="#1f2418", fg=fg, highlightthickness=0,
                     command=lambda v: state["ctl"] and setattr(state["ctl"], "max_step", int(v)))
    speed.set(6)
    speed.pack(side="left", padx=8)
    note = tk.Label(root, text="", fg="#ffb000", bg="#14170f", wraplength=560)

    def home():
        ctl = state["ctl"]
        ctl.home()
        for axis in ("pan", "tilt"):
            state["scales"][axis].set(ctl.target[axis])

    def mark(which):
        ctl = state["ctl"]
        cal = ctl.mark(which)
        if cal:
            tilt_calibration.save(cal)
            log(f"{time.strftime('%H:%M:%S')} SAVED calibration {cal}")
            note.config(text=f"saved: tilt range {cal['window'][0]} to {cal['window'][1]}, the bottom of the picture is servo "
                             f"angle {cal['window'][1] if cal['sign'] > 0 else cal['window'][0]}", fg="#b6e23a")
        else:
            note.config(text="marks: " + ", ".join(f"{k} = {v}" for k, v in ctl.marks.items()), fg="#ffb000")

    buttons = tk.Frame(root, bg="#14170f")
    buttons.pack(pady=8)
    tk.Button(buttons, text="Home", command=home).grid(row=0, column=0, padx=6)
    tk.Button(buttons, text="Mark tilt TOP (camera highest safe)", command=lambda: mark("top")).grid(row=0, column=1, padx=6)
    tk.Button(buttons, text="Mark tilt BOTTOM (camera lowest safe)", command=lambda: mark("bottom")).grid(row=0, column=2, padx=6)
    note.pack(pady=4)
    tk.Label(root, text="Arrow keys: 1 degree   PageUp/PageDown: 10   Esc: quit", fg=dim, bg="#14170f").pack()
    root.bind("<Escape>", lambda e: root.destroy())

    def tick():
        ctl = state["ctl"]
        if ctl:
            ctl.update()
            for axis in ("pan", "tilt"):
                state["labels"][axis].config(text=f"{ctl.sent[axis]}")
        root.after(UPDATE_MS, tick)

    try:
        gimbal = Gimbal(port, tilt_limits=TILT_RANGE)        # no limits: this tool is for finding out what the servos do
        pan, tilt = gimbal.status()
    except Exception as e:
        status.config(text=f"could not open {port}: {e}", fg="#ff4b2b")
        root.mainloop()
        return
    state["ctl"] = SliderControl(gimbal, pan=pan, tilt=tilt, log=log)
    state["scales"]["pan"].set(pan)
    state["scales"]["tilt"].set(tilt)
    status.config(text=f"connected on {port}; the ESP started at pan {pan}, tilt {tilt}. Nothing moves until you drag a slider.")
    log(f"{time.strftime('%H:%M:%S')} opened {port}, start pan {pan} tilt {tilt}")
    tick()
    root.mainloop()
    log(f"{time.strftime('%H:%M:%S')} closed; marks {state['ctl'].marks}")
    gimbal.close()


if __name__ == "__main__":
    run_window(os.environ.get("GIMBAL", "COM7"))
