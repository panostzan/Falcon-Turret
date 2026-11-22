"""Turns the target's position in the image into gimbal angles.

Two modes:
  map     The camera stays still and the turret points where the target is. The picture is mapped
          linearly onto the servos' travel: left edge -> pan 0, center -> 90, right edge -> 180 (and
          top edge -> tilt 90, center -> 135, bottom edge -> 180). The gimbal goes straight to that angle
          and stays there. No accumulation: same target position -> same angle.
  follow  The camera is mounted ON the gimbal. Each update turns the gimbal a fraction of the remaining
          error until the target is centered.
"""
import math

import ease as easing
from gimbal import CENTER, PAN_RANGE, TILT_RANGE

# Logitech C270: 60 degree diagonal field of view, 4:3 picture.
DIAGONAL_FOV = 60.0
_TAN_HALF_DIAG = math.tan(math.radians(DIAGONAL_FOV / 2))
H_FOV = 2 * math.degrees(math.atan(_TAN_HALF_DIAG * 4 / 5))   # about 49.6 degrees
V_FOV = 2 * math.degrees(math.atan(_TAN_HALF_DIAG * 3 / 5))   # about 38.2 degrees


def pixel_error_to_degrees(err, fov):
    """err is the target's offset from the image center as a fraction of the half-frame (-1..1).
    Returns the angle between the camera's axis and the target, in degrees (pinhole camera model)."""
    return math.degrees(math.atan(err * math.tan(math.radians(fov / 2))))


def clamp(value, lo_hi):
    return max(lo_hi[0], min(lo_hi[1], value))


class Aimer:
    """errors ex, ey are -1..1 (+x = target right of center, +y = below center).

    All resulting angles are clamped to the servo ranges (pan 0-180, tilt 90-180). `sign` flips an axis
    whose servo turns the opposite way. Errors smaller than `deadband` degrees are ignored.

    map mode:    gimbal angle = center angle + sign * offset * (span / 2), where offset is the target's
                 position across the picture (-1..1) and `span` is the servo travel the whole picture
                 covers (pan 180, tilt 90 by default: the full range). Lightly smoothed (`smooth`:
                 weight of the newest reading) so it doesn't jitter.
    follow mode: each update turns the gimbal by `gain` times the remaining angle error, at most
                 `max_step` degrees. The picture arrives a few frames late, so gains above ~0.3 oscillate.
    """

    def __init__(self, gimbal, mode="map", gain=(0.2, 0.2), deadband=1.0, max_step=8.0, smooth=0.35, fast_ramp=12.0,
                 map_start=2.0, map_stop=0.6, span=(PAN_RANGE[1] - PAN_RANGE[0], TILT_RANGE[1] - TILT_RANGE[0]),
                 tilt_window=None, tilt_enabled=True, ease=None):
        """tilt_window=(lo, hi): the tilt servo's usable travel when the whole range does not fit the mechanics. The
        picture's height maps onto just this window, its middle is the tilt 'center', and the aim never leaves it."""
        self.gimbal = gimbal
        self.ease = ease              # None = jump straight to the goal; {"vmax": deg/s, "accel": deg/s^2} = glide there (see tick)
        self._ease_goal = {"pan": None, "tilt": None}
        self._ease_pos = {"pan": None, "tilt": None}      # where each axis is, in fractions of a degree (the servo only gets whole ones)
        self._ease_vel = {"pan": 0.0, "tilt": 0.0}
        self._ease_t = None
        self.tilt_enabled = tilt_enabled      # False = pan only: the tilt servo is never commanded
        lo, hi = tilt_window if tilt_window else TILT_RANGE
        self.tilt_clamp = (lo, hi)
        self.tilt_center = (lo + hi) / 2
        if tilt_window:
            span = (span[0], hi - lo)
        self.mode = mode
        self.gain = {"pan": gain[0], "tilt": gain[1]}
        self.sign = {"pan": 1, "tilt": 1}
        self.deadband = deadband
        self.max_step = max_step
        self.smooth = smooth       # weight of a new reading while the goal is nearly still (calm)
        self.fast_ramp = fast_ramp  # a goal change this many degrees or more is followed instantly
        self.span = {"pan": span[0], "tilt": span[1]}   # servo degrees covered by the full picture width/height
        # Hysteresis so the servo does not hunt on pixel noise: an axis that is holding still starts moving only when
        # the goal is more than map_start degrees away, and once moving keeps going until within map_stop degrees.
        self.map_start, self.map_stop = map_start, map_stop
        self._moving = {"pan": False, "tilt": False}
        self._goal = None            # smoothed map-mode target (pan, tilt)
        self.settled = False         # True when the last update needed no movement: the gimbal is on target

    def reset(self):
        """Forget the smoothed goal (call when the target was lost, so a new target starts fresh)."""
        self._goal = None
        self._moving = {"pan": False, "tilt": False}
        self._ease_goal = {"pan": None, "tilt": None}      # a lost target: stop gliding, stay where we are
        self._ease_vel = {"pan": 0.0, "tilt": 0.0}

    def set_mode(self, mode):
        if mode not in ("map", "follow"):
            raise ValueError(mode)
        self.mode, self._goal = mode, None

    def update(self, ex, ey):
        """Aim at the target; returns the gimbal's (pan, tilt) afterwards."""
        return self._map(ex, ey) if self.mode == "map" else self._follow(ex, ey)

    def _map(self, ex, ey):
        goal = (CENTER[0] + self.sign["pan"] * ex * self.span["pan"] / 2,
                self.tilt_center + self.sign["tilt"] * ey * self.span["tilt"] / 2)
        if self._goal is None:
            self._goal = goal
        else:
            # speed-dependent smoothing: heavy while the goal barely changes (kills pixel noise), none for big moves
            self._goal = tuple(
                a * g + (1 - a) * old
                for g, old, a in ((g, old, min(1.0, self.smooth + (1 - self.smooth) * abs(g - old) / self.fast_ramp))
                                  for g, old in zip(goal, self._goal)))
        pan, tilt = (clamp(self._goal[0], PAN_RANGE), clamp(self._goal[1], self.tilt_clamp))
        # only command an axis once it is off by more than the deadband, so a still target means a still servo
        new_pan = pan if self._gate("pan", abs(pan - self.gimbal.pan)) else None
        new_tilt = tilt if (self.tilt_enabled and self._gate("tilt", abs(tilt - self.gimbal.tilt))) else None
        self.settled = new_pan is None and new_tilt is None
        if self.ease:                      # glide: remember the goals; tick() (called ~50 times a second) does the moving
            if new_pan is not None:
                self._ease_goal["pan"] = new_pan
            if new_tilt is not None:
                self._ease_goal["tilt"] = new_tilt
            return self.gimbal.pan, self.gimbal.tilt
        return self.gimbal.move_to(pan=new_pan, tilt=new_tilt)

    def tick(self, now):
        """Easing only: move each axis one small, smooth step toward its goal. Call about 50 times a second. Whole degrees reach
        the servo; the fractions are kept here so slow gliding still advances."""
        if not self.ease:
            return
        dt = 0.0 if self._ease_t is None else min(0.1, max(0.0, now - self._ease_t))
        self._ease_t = now
        if dt <= 0:
            return
        moves = {}
        for axis, held in (("pan", self.gimbal.pan), ("tilt", self.gimbal.tilt)):
            goal = self._ease_goal[axis]
            if goal is None:
                self._ease_pos[axis] = None
                continue
            pos = self._ease_pos[axis]
            if pos is None or abs(pos - held) > 1.0:        # something else moved the axis (home, scan): start from where it really is
                pos, self._ease_vel[axis] = float(held), 0.0
            pos, self._ease_vel[axis] = easing.step(pos, self._ease_vel[axis], float(goal), dt, self.ease["vmax"], self.ease["accel"])
            self._ease_pos[axis] = pos
            if pos == goal:
                self._ease_goal[axis] = None
            if round(pos) != held:
                moves[axis] = pos
        if moves:
            self.gimbal.move_to(pan=moves.get("pan"), tilt=moves.get("tilt"))

    def _gate(self, axis, err):
        """Hysteresis: True when this axis should be commanded to move toward its goal."""
        if self._moving[axis]:
            self._moving[axis] = err >= self.map_stop
        elif err >= self.map_start:
            self._moving[axis] = True
        return self._moving[axis]

    def _follow_step(self, axis, err, fov):
        degrees = pixel_error_to_degrees(err, fov)
        if abs(degrees) < self.deadband:
            return 0.0
        step = self.sign[axis] * self.gain[axis] * degrees
        return max(-self.max_step, min(self.max_step, step))

    def _follow(self, ex, ey):
        step_pan = self._follow_step("pan", ex, H_FOV)
        step_tilt = self._follow_step("tilt", ey, V_FOV) if self.tilt_enabled else 0.0
        self.settled = step_pan == 0 and step_tilt == 0
        pan = clamp(self.gimbal.pan + step_pan, PAN_RANGE)
        tilt = clamp(self.gimbal.tilt + step_tilt, self.tilt_clamp)
        return self.gimbal.move_to(pan=pan, tilt=tilt)

    def home(self):
        """Go to the middle of the pan range and the middle of the tilt window."""
        self._ease_goal = {"pan": None, "tilt": None}
        self._ease_vel = {"pan": 0.0, "tilt": 0.0}
        return self.gimbal.move_to(pan=CENTER[0], tilt=self.tilt_center)

    def flip(self, axis):
        self.sign[axis] *= -1
        return self.sign[axis]
