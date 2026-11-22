"""Sliders to move the pan, tilt, lift (platform) and aux (fourth) servos by hand and see the angles being commanded.

    python tools/servo_sliders.py        (the dashboard server must not be tracking: it would be holding the port)

  Pan, Tilt, Lift and Aux sliders   drag to move; the number is the angle being COMMANDED (pan, tilt and aux 0-180, lift 0-150).
                               Pan and tilt use the same angles as the ESP32 setup did, so their numbers mean the same.
  Arrow keys                   with a slider selected, nudge it by 1 degree (PageUp/PageDown: 10)
  Centre                       pan 90, tilt 22, lift 90, and energises all three (they jump there: keep the arms off for the first test)
  Release all                  every servo goes limp (no pulses). Dragging a slider energises that servo again.
  Max speed                    the most a servo is told to move per update, so a sudden drag cannot slam it into a stop
  Mark tilt TOP / BOTTOM       with the camera at its highest / lowest SAFE position, press the button: it saves the tilt range
                               and direction for the dashboard (laptop/tilt_calibration.json)
  Mark lift LOW / HIGH         the same for the platform servo: saved to laptop/lift_calibration.json
  Oscillator (kHz)             the servo driver's clock, 25000 nominal. If every angle is off by the same proportion (say the
                               ends reach 8 degrees too far), nudge this. It is a trim, not something to change every day.
The Feather's servos are LIMP until the first command to each one, so nothing moves when this window opens.
Every command is also written to captures/servo_sliders.log. Esc quits (the servos stay where they are).
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "laptop"))
from gimbal import AUX_RANGE, CENTER, LIFT_RANGE, PAN_RANGE, TILT_RANGE   # noqa: E402
import tilt_calibration                            # noqa: E402

LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "captures", "servo_sliders.log")
LIFT_CAL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "laptop", "lift_calibration.json")
LIFT_HOME = 90                                    # the lift goes 0 to 150 (a software limit, see gimbal.LIFT_RANGE)
UPDATE_MS = 30
AXES = ("pan", "tilt", "lift", "aux")
AUX_HOME = 90                                     # the fourth servo (PCA9685 channel 3); the Centre button does NOT move it
RANGES = {"pan": PAN_RANGE, "tilt": TILT_RANGE, "lift": LIFT_RANGE, "aux": AUX_RANGE}


class SliderControl:
    """The logic without a window: the sliders set TARGETS; every update moves each servo toward its target by at most
    `max_step` degrees and sends only what changed. Testable with a fake gimbal."""

    def __init__(self, gimbal, pan=CENTER[0], tilt=CENTER[1], lift=LIFT_HOME, max_step=6, log=None, aux=AUX_HOME):
        self.g, self.max_step, self.log = gimbal, max_step, log
        self.target = {"pan": pan, "tilt": tilt, "lift": lift, "aux": aux}
        self.sent = dict(self.target)
        self.marks = {}

    @staticmethod
    def _clamp(axis, v):
        lo, hi = RANGES[axis]
        return max(lo, min(hi, int(round(v))))

    def set_target(self, axis, value):
        self.target[axis] = self._clamp(axis, value)

    def nudge(self, axis, delta):
        self.set_target(axis, self.target[axis] + delta)

    def centre(self):
        """Go to the start positions and energise all three servos (the Feather's C command)."""
        self.target.update(pan=CENTER[0], tilt=CENTER[1], lift=LIFT_HOME)
        self.sent.update(self.target)
        self.g.center()
        if self.log:
            self.log(f"{time.strftime('%H:%M:%S')} centre: pan {CENTER[0]} tilt {CENTER[1]} lift {LIFT_HOME}")

    def release(self):
        """Every servo limp. The sliders stay where they are; moving one energises that servo again."""
        self.g.release()
        if self.log:
            self.log(f"{time.strftime('%H:%M:%S')} released all servos (limp)")

    def set_oscillator(self, khz):
        self.g.set_oscillator(khz)
        if self.log:
            self.log(f"{time.strftime('%H:%M:%S')} oscillator {khz} kHz")

    def update(self):
        """One step: move toward the targets. Returns True if anything was sent."""
        moved = False
        for axis in AXES:
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
        """Mark the current angle of the servo this end belongs to: tilt top/bottom or lift low/high. Returns the saved
        calibration dict when a pair is complete, else None."""
        axis = "tilt" if which in ("top", "bottom") else "lift"
        self.marks[which] = self.sent[axis]
        if self.log:
            self.log(f"{time.strftime('%H:%M:%S')} MARK {axis} {which} = {self.sent[axis]}")
        if axis == "tilt" and "top" in self.marks and "bottom" in self.marks:
            return tilt_calibration.from_marks(self.marks["top"], self.marks["bottom"])
        if axis == "lift" and "low" in self.marks and "high" in self.marks:
            lo, hi = sorted((self.marks["low"], self.marks["high"]))
            return {"low": lo, "high": hi} if hi - lo >= 4 else None
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
    root.geometry("660x680")
    fg, dim, accent = "#dfe6cf", "#8b9777", "#b6e23a"
    status = tk.Label(root, text="opening the port...", fg=fg, bg="#14170f", wraplength=620)
    status.pack(pady=(10, 4))
    state = {"ctl": None, "labels": {}, "scales": {}}

    def make_slider(axis, caption):
        frame = tk.Frame(root, bg="#14170f")
        frame.pack(fill="x", padx=20, pady=5)
        tk.Label(frame, text=caption, width=7, fg=accent, bg="#14170f", font=("Consolas", 13, "bold"), anchor="w").pack(side="left")
        big = tk.Label(frame, text="--", width=5, fg=accent, bg="#14170f", font=("Consolas", 30, "bold"))
        lo, hi = RANGES[axis]
        scale = tk.Scale(frame, from_=lo, to=hi, orient="horizontal", length=380, showvalue=False,
                         bg="#1f2418", fg=fg, troughcolor="#0a0d07", highlightthickness=0,
                         command=lambda v, a=axis: state["ctl"] and state["ctl"].set_target(a, float(v)))
        scale.pack(side="left", padx=8)
        big.pack(side="left")
        state["labels"][axis], state["scales"][axis] = big, scale

    make_slider("pan", "PAN")
    make_slider("tilt", "TILT")
    make_slider("lift", "LIFT")
    make_slider("aux", "AUX")

    speed_row = tk.Frame(root, bg="#14170f")
    speed_row.pack(pady=4)
    tk.Label(speed_row, text="Max speed (degrees per update)", fg=dim, bg="#14170f").pack(side="left")
    speed = tk.Scale(speed_row, from_=1, to=20, orient="horizontal", length=160, bg="#1f2418", fg=fg, highlightthickness=0,
                     command=lambda v: state["ctl"] and setattr(state["ctl"], "max_step", int(v)))
    speed.set(6)
    speed.pack(side="left", padx=8)
    note = tk.Label(root, text="", fg="#ffb000", bg="#14170f", wraplength=620)

    def sync_sliders():
        ctl = state["ctl"]
        for axis in AXES:
            state["scales"][axis].set(ctl.target[axis])

    def centre():
        state["ctl"].centre()
        sync_sliders()

    def release():
        state["ctl"].release()
        note.config(text="all servos released (limp). Drag a slider to energise that one again.", fg="#ffb000")

    def mark(which):
        ctl = state["ctl"]
        cal = ctl.mark(which)
        if cal and which in ("top", "bottom"):
            tilt_calibration.save(cal)
            log(f"{time.strftime('%H:%M:%S')} SAVED tilt calibration {cal}")
            note.config(text=f"saved: tilt range {cal['window'][0]} to {cal['window'][1]}, the bottom of the picture is servo "
                             f"angle {cal['window'][1] if cal['sign'] > 0 else cal['window'][0]}", fg="#b6e23a")
        elif cal:
            with open(LIFT_CAL, "w", encoding="utf-8") as f:
                json.dump(cal, f, indent=2)
            log(f"{time.strftime('%H:%M:%S')} SAVED lift calibration {cal}")
            note.config(text=f"saved: lift range {cal['low']} to {cal['high']}", fg="#b6e23a")
        else:
            note.config(text="marks: " + ", ".join(f"{k} = {v}" for k, v in ctl.marks.items()), fg="#ffb000")

    def apply_osc():
        try:
            khz = int(osc_box.get())
            if not 20000 <= khz <= 30000:
                raise ValueError
            state["ctl"].set_oscillator(khz)
            note.config(text=f"oscillator set to {khz} kHz", fg="#b6e23a")
        except ValueError:
            note.config(text="the oscillator must be a whole number between 20000 and 30000", fg="#ff4b2b")

    row1 = tk.Frame(root, bg="#14170f")
    row1.pack(pady=6)
    tk.Button(row1, text="Centre pan/tilt/lift", command=centre).grid(row=0, column=0, padx=6)
    tk.Button(row1, text="Release all (limp)", command=release).grid(row=0, column=1, padx=6)
    row2 = tk.Frame(root, bg="#14170f")
    row2.pack(pady=2)
    tk.Button(row2, text="Mark tilt TOP (camera highest safe)", command=lambda: mark("top")).grid(row=0, column=0, padx=6)
    tk.Button(row2, text="Mark tilt BOTTOM (camera lowest safe)", command=lambda: mark("bottom")).grid(row=0, column=1, padx=6)
    row3 = tk.Frame(root, bg="#14170f")
    row3.pack(pady=2)
    tk.Button(row3, text="Mark lift LOW", command=lambda: mark("low")).grid(row=0, column=0, padx=6)
    tk.Button(row3, text="Mark lift HIGH", command=lambda: mark("high")).grid(row=0, column=1, padx=6)
    row4 = tk.Frame(root, bg="#14170f")
    row4.pack(pady=6)
    tk.Label(row4, text="Oscillator (kHz)", fg=dim, bg="#14170f").grid(row=0, column=0, padx=4)
    osc_box = tk.Entry(row4, width=8)
    osc_box.insert(0, "25000")
    osc_box.grid(row=0, column=1, padx=4)
    tk.Button(row4, text="Apply", command=apply_osc).grid(row=0, column=2, padx=4)
    note.pack(pady=4)
    tk.Label(root, text="Arrow keys: 1 degree   PageUp/PageDown: 10   Esc: quit", fg=dim, bg="#14170f").pack()
    root.bind("<Escape>", lambda e: root.destroy())

    def tick():
        ctl = state["ctl"]
        if ctl:
            ctl.update()
            for axis in AXES:
                state["labels"][axis].config(text=f"{ctl.sent[axis]}")
        root.after(UPDATE_MS, tick)

    try:
        gimbal = Gimbal(port, tilt_limits=TILT_RANGE, lift_limits=LIFT_RANGE)   # no limits on tilt (this tool finds the ends); lift stops at 150
        pan, tilt = gimbal.status()
    except Exception as e:
        status.config(text=f"could not open {port}: {e}", fg="#ff4b2b")
        root.mainloop()
        return
    state["ctl"] = SliderControl(gimbal, pan=pan, tilt=tilt, lift=gimbal.lift, aux=gimbal.aux, log=log)
    sync_sliders()
    status.config(text=f"connected on {port}. The servos are limp until you drag a slider (or press Centre). "
                       f"Firmware reports pan {pan}, tilt {tilt}, lift {gimbal.lift}, aux {gimbal.aux}.")
    log(f"{time.strftime('%H:%M:%S')} opened {port}, start pan {pan} tilt {tilt} lift {gimbal.lift} aux {gimbal.aux}")
    tick()
    root.mainloop()
    log(f"{time.strftime('%H:%M:%S')} closed; marks {state['ctl'].marks}")
    gimbal.close()


if __name__ == "__main__":
    run_window(os.environ.get("GIMBAL", "COM8"))
