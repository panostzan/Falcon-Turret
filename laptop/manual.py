"""Manual mode: move each mechanical part by hand, slowly, from the Servos page (the sliders).

The dashboard server is the only owner of the Feather's USB port. In manual mode the sliders set TARGETS and this class walks every
servo toward its target a couple of degrees at a time, at a fixed gentle speed, so a sudden drag never throws a part across its range.

Parts: pan, tilt, lift (the platform), roof (the aux servo).

Two rules keep the platform and the roof from meeting, the same idea as roof.py:
    * the platform can only rise (above LIFT_LOW_MAX) while the roof is open (at OPEN_ENOUGH or lower);
    * the roof can only close (above OPEN_ENOUGH) while the platform is down (at LIFT_LOW_MAX or lower).
The rules look at where each part REALLY is (the last angle sent), not where its slider is.

The rest position is ASSUMED, because nothing reports the true angles: platform down (0), roof closed (140), pan and tilt at home.
Entering manual mode stops tracking first (which closes everything properly), so the assumption holds. Nothing is sent until a
slider is moved, and the Feather's servos stay limp until then.
"""
import os
import threading

from gimbal import AUX_RANGE, CENTER, LIFT_RANGE, PAN_RANGE

AXES = ("pan", "tilt", "lift", "roof")
GIMBAL_NAME = {"pan": "pan", "tilt": "tilt", "lift": "lift", "roof": "aux"}     # what the gimbal driver calls each part
ROOF_CLOSED = AUX_RANGE[1]
MAX_STEP = int(os.environ.get("TURRET_MANUAL_STEP", 6))    # degrees per update (about every 30 ms: about 200 degrees/s, the pace of the old slider window)
OPEN_ENOUGH = int(os.environ.get("TURRET_ROOF_CLEAR", 100))    # the roof counts as open (platform may rise) at this angle or lower
LIFT_LOW_MAX = int(os.environ.get("TURRET_LIFT_LOW", 10))      # the platform counts as down at this angle or lower


def rest_position(tilt_range):
    tilt = max(tilt_range[0], min(tilt_range[1], CENTER[1]))
    return {"pan": CENTER[0], "tilt": tilt, "lift": 0, "roof": ROOF_CLOSED}


def default_ranges(tilt_range):
    return {"pan": tuple(PAN_RANGE), "tilt": tuple(tilt_range), "lift": LIFT_RANGE, "roof": tuple(AUX_RANGE)}


class ManualControl:
    def __init__(self, gimbal, tilt_range, max_step=MAX_STEP):
        self.g, self.max_step = gimbal, max_step
        self.ranges = default_ranges(tilt_range)
        self.sent = rest_position(tilt_range)
        self.target = dict(self.sent)
        self.locked = False                       # True while parking: the sliders are ignored
        self._lock = threading.RLock()

    def set_target(self, axis, value):
        """A slider moved. Returns None, or a sentence saying why the move was limited or refused."""
        with self._lock:
            if axis not in AXES:
                return "unknown part"
            if self.locked:
                return "Returning to Auto: the sliders are locked until it is done."
            lo, hi = self.ranges[axis]
            v = max(lo, min(hi, int(round(value))))
            if axis == "lift" and v > LIFT_LOW_MAX and self.sent["roof"] > OPEN_ENOUGH:
                self.target["lift"] = min(self.target["lift"], LIFT_LOW_MAX)
                return "Open the roof first: the platform can only rise once the roof is open."
            if axis == "roof" and v > OPEN_ENOUGH and (self.sent["lift"] > LIFT_LOW_MAX or self.target["lift"] > LIFT_LOW_MAX):
                return "Lower the platform first: the roof can only close once the platform is down."
            self.target[axis] = v
            return None

    def update(self):
        """One step toward the targets (call about every 30 ms). Returns True if anything was sent."""
        moved = False
        with self._lock:
            for axis in AXES:
                gap = self.target[axis] - self.sent[axis]
                if not gap:
                    continue
                new = self.sent[axis] + max(-self.max_step, min(self.max_step, gap))
                if axis == "roof" and new > OPEN_ENOUGH and self.sent["lift"] > LIFT_LOW_MAX:
                    continue                                    # the roof waits for the platform to come down
                if axis == "lift" and new > LIFT_LOW_MAX and self.sent["roof"] > OPEN_ENOUGH:
                    continue                                    # the platform waits for the roof to open
                self.g.move_to(**{GIMBAL_NAME[axis]: new})
                self.sent[axis] = new
                moved = True
        return moved

    def park(self):
        """Back to the rest position, in a safe order (the two rules above make the roof wait for the platform). Locks the sliders."""
        with self._lock:
            self.locked = True
            self.target = rest_position((self.ranges["tilt"][0], self.ranges["tilt"][1]))

    @property
    def settled(self):
        with self._lock:
            return self.sent == self.target

    def info(self):
        with self._lock:
            return {"sent": dict(self.sent), "target": dict(self.target), "ranges": {k: list(v) for k, v in self.ranges.items()},
                    "locked": self.locked, "settled": self.sent == self.target}
