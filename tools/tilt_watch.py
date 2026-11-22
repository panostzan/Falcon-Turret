"""A small window for finding the tilt servo's real range: it shows the angle being COMMANDED, live, and lets you move it.

    python tools/tilt_watch.py            opens the window (the dashboard server must not be tracking: it would hold the port)

  Up / Down arrows      move 1 degree           PageUp / PageDown   move 5 degrees        Home   go to 22
  Sweep button          goes back and forth between the Low and High boxes, 1 degree at a time
  "Mark low" / "Mark high"   write down the current angle as the end you found (shown at the bottom and in the log)
  Esc                   quit (the servo stays where it is)

The firmware only accepts tilt 0-60. Watch the servo, and note the commanded angle where it starts to move, where it jams
or hums, and which end of the picture each end is. Every command is also written to captures/tilt_watch.log.
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "laptop"))
from gimbal import TILT_RANGE   # noqa: E402

LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "captures", "tilt_watch.log")


class TiltControl:
    """The logic without any window, so it can be tested: current angle, limits, jogging and the back-and-forth sweep."""

    def __init__(self, gimbal, start=22, lo=TILT_RANGE[0], hi=TILT_RANGE[1], log=None):
        self.g, self.lo, self.hi, self.angle, self.direction = gimbal, lo, hi, start, 1
        self.log, self.sweeping, self.marks = log, False, {}

    def set(self, angle):
        angle = max(TILT_RANGE[0], min(TILT_RANGE[1], int(round(angle))))
        self.g.move_to(tilt=angle)
        self.angle = angle
        if self.log:
            self.log(f"{time.strftime('%H:%M:%S')} commanded tilt {angle}")
        return angle

    def jog(self, delta):
        self.sweeping = False
        return self.set(self.angle + delta)

    def set_limits(self, lo, hi):
        """Low and high ends of the sweep. The firmware only takes 90-180, so anything outside is clamped to that range
        (typing 0 and 90 gives a sweep of 90 to 90, which is no sweep at all: sweep_step then refuses to move)."""
        lo, hi = int(lo), int(hi)
        clamp = lambda v: max(TILT_RANGE[0], min(TILT_RANGE[1], v))
        self.lo, self.hi = clamp(min(lo, hi)), clamp(max(lo, hi))

    def sweep_step(self):
        """One step of the back-and-forth sweep: ALWAYS exactly one degree (or none). Turns around at the limits. If the
        current angle is outside the limits it walks back toward them one degree at a time, it never jumps. With no room
        to sweep (low equals high) it stops sweeping instead of sitting on one angle."""
        if self.hi - self.lo < 2:
            self.sweeping = False
            if self.log:
                self.log(f"{time.strftime('%H:%M:%S')} sweep stopped: low and high are {self.lo} and {self.hi}")
            return self.angle
        if self.angle > self.hi:
            self.direction = -1
        elif self.angle < self.lo:
            self.direction = 1
        elif self.angle == self.hi:
            self.direction = -1
        elif self.angle == self.lo:
            self.direction = 1
        return self.set(self.angle + self.direction)

    def mark(self, which):
        self.marks[which] = self.angle
        if self.log:
            self.log(f"{time.strftime('%H:%M:%S')} MARK {which} = {self.angle}")


def run_window(port):
    import tkinter as tk
    from gimbal import Gimbal

    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    logfile = open(LOG, "a", encoding="utf-8")

    def log(text):
        logfile.write(text + "\n")
        logfile.flush()

    root = tk.Tk()
    root.title("Tilt watch")
    root.configure(bg="#14170f")
    root.geometry("560x420")
    big = tk.Label(root, text="…", font=("Consolas", 64, "bold"), fg="#b6e23a", bg="#14170f")
    big.pack(pady=(14, 0))
    tk.Label(root, text="commanded tilt, degrees (the firmware accepts 0 to 60)", fg="#8b9777", bg="#14170f").pack()
    canvas = tk.Canvas(root, width=500, height=46, bg="#0a0d07", highlightthickness=0)
    canvas.pack(pady=10)
    status = tk.Label(root, text="opening the port (this resets the ESP)...", fg="#dfe6cf", bg="#14170f")
    status.pack()
    row = tk.Frame(root, bg="#14170f")
    row.pack(pady=6)
    tk.Label(row, text="Low", fg="#dfe6cf", bg="#14170f").grid(row=0, column=0)
    low_box = tk.Entry(row, width=5)
    low_box.insert(0, str(TILT_RANGE[0]))
    low_box.grid(row=0, column=1, padx=4)
    tk.Label(row, text="High", fg="#dfe6cf", bg="#14170f").grid(row=0, column=2)
    high_box = tk.Entry(row, width=5)
    high_box.insert(0, str(TILT_RANGE[1]))
    high_box.grid(row=0, column=3, padx=4)
    marks = tk.Label(root, text="marks: none yet", fg="#ffb000", bg="#14170f")
    state = {"ctl": None}

    def draw():
        ctl = state["ctl"]
        canvas.delete("all")
        x = lambda a: 10 + (a - TILT_RANGE[0]) / (TILT_RANGE[1] - TILT_RANGE[0]) * 480
        canvas.create_rectangle(x(ctl.lo), 12, x(ctl.hi), 34, fill="#1f2418", outline="#333b27")
        for a in range(TILT_RANGE[0], TILT_RANGE[1] + 1, 10):
            canvas.create_line(x(a), 34, x(a), 42, fill="#8b9777")
            canvas.create_text(x(a), 6, text=str(a), fill="#8b9777", font=("Consolas", 8))
        canvas.create_line(x(ctl.angle), 8, x(ctl.angle), 40, fill="#b6e23a", width=3)
        big.config(text=f"{ctl.angle}°")
        marks.config(text="marks: " + (", ".join(f"{k} = {v}" for k, v in ctl.marks.items()) or "none yet"))

    def read_limits():
        try:
            state["ctl"].set_limits(low_box.get(), high_box.get())
        except ValueError:
            pass

    def tick():
        ctl = state["ctl"]
        if ctl and ctl.sweeping:
            read_limits()
            ctl.sweep_step()
            draw()
        root.after(100, tick)

    def jog(d):
        return lambda e=None: (state["ctl"].jog(d), draw())

    def toggle_sweep():
        ctl = state["ctl"]
        ctl.sweeping = not ctl.sweeping
        sweep_btn.config(text="Stop sweep" if ctl.sweeping else "Sweep back and forth")

    def mark(which):
        state["ctl"].mark(which)
        draw()

    sweep_btn = tk.Button(root, text="Sweep back and forth", command=toggle_sweep)
    sweep_btn.pack(pady=4)
    both = tk.Frame(root, bg="#14170f")
    both.pack()
    tk.Button(both, text="Mark low", command=lambda: mark("low")).grid(row=0, column=0, padx=6)
    tk.Button(both, text="Mark high", command=lambda: mark("high")).grid(row=0, column=1, padx=6)
    marks.pack(pady=6)
    tk.Label(root, text="Up/Down = 1 degree   PageUp/PageDown = 5   Home = 22   Esc = quit", fg="#8b9777", bg="#14170f").pack()
    for key, d in (("<Up>", 1), ("<Down>", -1), ("<Prior>", 5), ("<Next>", -5)):
        root.bind(key, jog(d))
    root.bind("<Home>", lambda e: (state["ctl"].jog(22 - state["ctl"].angle), draw()))
    root.bind("<Escape>", lambda e: root.destroy())

    try:
        gimbal = Gimbal(port)
        start = gimbal.status()[1]
    except Exception as e:
        status.config(text=f"could not open {port}: {e}", fg="#ff4b2b")
        root.mainloop()
        return
    state["ctl"] = TiltControl(gimbal, start=start, log=log)
    status.config(text=f"connected on {port}; the ESP booted at tilt {start}. Nothing moves until you press a key.")
    log(f"{time.strftime('%H:%M:%S')} opened {port}, boot tilt {start}")
    draw()
    tick()
    root.mainloop()
    log(f"{time.strftime('%H:%M:%S')} closed; marks {state['ctl'].marks}")
    print("marks:", state["ctl"].marks)
    gimbal.close()


if __name__ == "__main__":
    run_window(os.environ.get("GIMBAL", "COM7"))
