"""Color tracker for one target (a teal box, a white bottle, any colored marker).

A color mask is rarely one solid blob: shading, glare and edges break an object into fragments. So the
mask is dilated to join fragments that are close together, each joined group becomes one candidate, and
the candidate that best matches the target (size, shape, nearness to where it was) wins. The returned box
is tight around the real mask pixels of that group, and the tracker follows its center.
"""
import cv2
import numpy as np

SCALE = 0.5            # work at half size for speed
MIN_AREA = 40          # mask pixels at half size
MAX_FRAC = 0.30        # a group covering more of the frame than this is a wall, not a target
SIZE_RANGE = (0.25, 4.0)   # accepted group area relative to the learned target size
MIN_FILL = 0.15        # mask pixels / box area; rejects very sparse scatter
JOIN = 15              # fragments closer than about this many pixels (half size) are joined
SMOOTH = 0.8           # weight of the newest box center (1 = no smoothing)
LOST_AFTER = 10        # forget the last position after this many frames without a match

# OpenCV HSV: hue 0-180, so cyan/teal sits near 90 and pure blue near 120.
PRESETS = {
    "teal": ((75, 70, 40), (105, 255, 255)),
    "neon blue": ((100, 120, 80), (125, 255, 255)),
}


class ColorTracker:
    def __init__(self):
        self.lo = self.hi = None
        self.ref_area = None
        self.last = None
        self.missed = 0
        self.debug_mask = None   # last mask at half size, for the dashboard's mask view

    @property
    def armed(self):
        return self.lo is not None

    def use_preset(self, name):
        """Track a known color without clicking. Size is learned from the first match."""
        lo, hi = PRESETS[name]
        self.lo, self.hi = np.array(lo), np.array(hi)
        self.ref_area, self.last, self.missed = None, None, 0

    def clear(self):
        self.lo = self.hi = self.ref_area = self.last = self.debug_mask = None

    def _prep(self, frame):
        return cv2.cvtColor(cv2.GaussianBlur(cv2.resize(frame, None, fx=SCALE, fy=SCALE), (5, 5), 0),
                            cv2.COLOR_BGR2HSV)

    def _mask(self, hsv):
        lo, hi = self.lo, self.hi
        if lo[0] < 0:   # hue wraps around red
            m = (cv2.inRange(hsv, np.array([0, lo[1], lo[2]]), hi) |
                 cv2.inRange(hsv, np.array([180 + lo[0], lo[1], lo[2]]), np.array([180, hi[1], hi[2]])))
        elif hi[0] > 180:
            m = (cv2.inRange(hsv, lo, np.array([180, hi[1], hi[2]])) |
                 cv2.inRange(hsv, np.array([0, lo[1], lo[2]]), np.array([hi[0] - 180, hi[1], hi[2]])))
        else:
            m = cv2.inRange(hsv, lo, hi)
        return cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))   # drop speckle

    def _groups(self, mask):
        """Join nearby fragments; return [(mask_pixels, (x, y, w, h))] with tight boxes."""
        joined = cv2.dilate(mask, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (JOIN, JOIN)))
        n, labels, stats, _ = cv2.connectedComponentsWithStats(joined)
        groups = []
        for i in range(1, n):
            x, y, w, h = stats[i, :4]
            region = mask[y:y + h, x:x + w] & (labels[y:y + h, x:x + w] == i).astype(np.uint8) * 255
            area = int(cv2.countNonZero(region))
            if area >= MIN_AREA:
                tx, ty, tw, th = cv2.boundingRect(cv2.findNonZero(region))
                groups.append((area, (x + tx, y + ty, tw, th)))
        return groups

    def select(self, frame, x, y, r=5):
        """Lock onto the object at full-frame pixel (x, y). Returns False if the click isn't on a
        reasonably sized object (e.g. it landed on a wall), leaving the tracker unarmed."""
        hsv = self._prep(frame)
        px, py = int(x * SCALE), int(y * SCALE)
        patch = hsv[max(0, py - r):py + r + 1, max(0, px - r):px + r + 1].reshape(-1, 3)
        h, s, v = (int(c) for c in np.median(patch, axis=0))
        if s < 50:   # white / gray: tight brightness band, low saturation only
            self.lo = np.array([0, 0, max(0, v - 35)])
            self.hi = np.array([180, min(255, s + 30), min(255, v + 35)])
        else:        # colored: hue band (wrapping around red) with generous saturation/brightness
            self.lo = np.array([h - 10, max(0, s - 70), max(0, v - 80)])
            self.hi = np.array([h + 10, 255, 255])
        mask = self._mask(hsv)
        max_area = MAX_FRAC * mask.size
        hit = [g for g in self._groups(mask)
               if g[1][0] - 6 <= px <= g[1][0] + g[1][2] + 6 and g[1][1] - 6 <= py <= g[1][1] + g[1][3] + 6]
        if not hit or max(g[0] for g in hit) > max_area:
            self.clear()
            return False
        self.ref_area, self.last, self.missed = float(max(g[0] for g in hit)), None, 0
        return True

    def find(self, frame):
        """Return (cx, cy, w, h): the target's bounding box in full-frame pixels, or None."""
        if not self.armed:
            return None
        mask = self._mask(self._prep(frame))
        self.debug_mask = mask
        max_area = MAX_FRAC * mask.size
        blobs = [(a, b) for a, b in self._groups(mask)
                 if a <= max_area
                 and (self.ref_area is None or SIZE_RANGE[0] * self.ref_area <= a <= SIZE_RANGE[1] * self.ref_area)
                 and a / max(1, b[2] * b[3]) >= MIN_FILL]
        if not blobs:
            self.missed += 1
            if self.missed > LOST_AFTER:
                self.last = None
            return None

        def score(blob):   # prefer groups near the previous position, then larger ones
            area, (x, y, w, h) = blob
            if self.last is None:
                return area
            d = np.hypot(x + w / 2 - self.last[0], y + h / 2 - self.last[1])
            return area / (1 + (d / 40) ** 2)

        area, (x, y, w, h) = max(blobs, key=score)
        cx, cy = x + w / 2, y + h / 2
        if self.last is not None:   # light smoothing of the box center
            cx = SMOOTH * cx + (1 - SMOOTH) * self.last[0]
            cy = SMOOTH * cy + (1 - SMOOTH) * self.last[1]
        self.last, self.missed = (cx, cy), 0
        self.ref_area = area if self.ref_area is None else 0.9 * self.ref_area + 0.1 * area   # follow size changes
        return (cx / SCALE, cy / SCALE, w / SCALE, h / SCALE)
