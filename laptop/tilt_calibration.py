"""The tilt calibration the operator made with tools/tilt_watch.py: which servo angles are the camera's highest and lowest safe
positions. It gives the tilt window (the aim never leaves it) and the direction (does the bottom of the picture mean the high
or the low servo angle?). Stored in laptop/tilt_calibration.json, which the dashboard reads when it starts."""
import json
import os

# TURRET_TILT_CALIBRATION overrides the file location (the tests point it at nothing, so a real calibration never changes them)
PATH = os.environ.get("TURRET_TILT_CALIBRATION") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "tilt_calibration.json")
MARGIN = 2          # degrees kept clear of each end the operator marked, so the arm never rests against a stop
MIN_SPAN = 6


def from_marks(top, bottom, margin=MARGIN):
    """top = servo angle with the camera at its highest safe position, bottom = at its lowest. Returns the calibration dict,
    or None when the two marks are too close together to be a real range."""
    lo, hi = min(top, bottom) + margin, max(top, bottom) - margin
    if hi - lo < MIN_SPAN:
        return None
    return {"window": [lo, hi], "sign": 1 if bottom > top else -1, "top_angle": top, "bottom_angle": bottom}


def save(cal, path=PATH):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cal, f, indent=2)


def load(path=PATH):
    """The saved calibration as (window tuple, sign), or (None, None) when there is none or it is unreadable."""
    try:
        with open(path, encoding="utf-8") as f:
            cal = json.load(f)
        lo, hi = (int(v) for v in cal["window"])
        sign = 1 if int(cal["sign"]) >= 0 else -1
        if 0 <= lo < hi <= 180:
            return (lo, hi), sign
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None, None
