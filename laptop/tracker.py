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
DEPTH_BAND = 0.12       # depth values (0..1) within this of the target's median count as 'the same depth'
DEPTH_JUMP = 0.20       # a median this far from the followed target's depth halves the depth score
SURE_FRAMES = 10       # frames in a row a target must be seen before confidence can reach 100%

# OpenCV HSV: hue 0-180, so cyan/teal sits near 90 and pure blue near 120.
PRESETS = {
    "teal": ((75, 70, 40), (105, 255, 255)),
    "blue": ((108, 150, 60), (132, 255, 255)),     # pure blue: hue 120 of 180 in OpenCV
}


class ColorTracker:
    def __init__(self):
        self.lo = self.hi = None
        self.ref_area = None
        self.last = None
        self.missed = 0
        self.debug_mask = None   # last mask at half size, for the dashboard's mask view
        self.preset = None       # 'teal', 'blue', 'picked' (clicked in the picture) or None
        self.confidence = 0.0    # 0..1 for the current match (0 when nothing is found)
        self.conf_parts = {}     # the factors behind it, for the diagnostics
        self.streak = 0          # consecutive frames the target has been found
        self.blob = None         # (x, y, w, h) of the matched group at half size, for the depth check
        self.depth_ref = None    # depth of the target we have been following (0..1, relative)

    @property
    def armed(self):
        return self.lo is not None

    def use_preset(self, name):
        """Track a known color without clicking. Size is learned from the first match."""
        lo, hi = PRESETS[name]
        self.lo, self.hi = np.array(lo), np.array(hi)
        self.ref_area, self.last, self.missed = None, None, 0
        self.preset, self.confidence, self.streak, self.depth_ref = name, 0.0, 0, None

    def clear(self):
        self.lo = self.hi = self.ref_area = self.last = self.debug_mask = self.preset = None
        self.confidence, self.streak, self.conf_parts, self.depth_ref, self.blob = 0.0, 0, {}, None, None

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
        self.preset, self.confidence, self.streak, self.depth_ref = "picked", 0.0, 0, None
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
            self.confidence, self.streak, self.blob = 0.0, 0, None
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

        best = max(blobs, key=score)
        area, (x, y, w, h) = best
        cx, cy = x + w / 2, y + h / 2
        self.blob = (x, y, w, h)
        self._rate(best, [b for b in blobs if b is not best])
        if self.last is not None:   # light smoothing of the box center
            cx = SMOOTH * cx + (1 - SMOOTH) * self.last[0]
            cy = SMOOTH * cy + (1 - SMOOTH) * self.last[1]
        self.last, self.missed = (cx, cy), 0
        self.ref_area = area if self.ref_area is None else 0.9 * self.ref_area + 0.1 * area   # follow size changes
        return (cx / SCALE, cy / SCALE, w / SCALE, h / SCALE)

    def _rate(self, best, others):
        """Confidence 0..1 that `best` is really the target. Five checks, each 0..1:
        fill (how solid the blob is), size (matches the learned size), unique (no similar rival blob),
        steady (did not jump from the last position) and seen (found for several frames in a row)."""
        area, (x, y, w, h) = best
        fill = min(1.0, max(0.0, (area / max(1, w * h) - MIN_FILL) / (0.6 - MIN_FILL)))
        size = 1.0 if self.ref_area is None else max(0.0, 1.0 - abs(area / self.ref_area - 1.0) / 0.6)
        unique = 1.0 - max((a for a, _ in others), default=0) / max(1, area)
        if self.last is None:
            steady = 1.0
        else:
            steady = 1.0 / (1.0 + (np.hypot(x + w / 2 - self.last[0], y + h / 2 - self.last[1]) / 60.0) ** 2)
        self.streak += 1
        seen = min(1.0, self.streak / SURE_FRAMES)
        self.conf_parts = {"fill": round(fill, 2), "size": round(size, 2), "unique": round(unique, 2),
                           "steady": round(steady, 2), "seen": round(seen, 2)}
        self.confidence = round(seen * (0.3 * fill + 0.25 * size + 0.25 * unique + 0.2 * steady), 3)

    def check_depth(self, depth01):
        """Lower the confidence if the depth map disagrees with the match. `depth01` is the relative depth (0 far,
        1 near), aligned with the picture the tracker just searched. Uses only the target's own color pixels:
          consistent: the share of them at about one depth (a box spanning a near and a far object scores low)
          steady: how close their median depth is to the depth of the target followed so far (a jump to another
                  object scores low).
        Confidence is multiplied by 0.5 + 0.5 * consistent * steady. Does nothing without a match or a depth map."""
        if self.blob is None or self.confidence <= 0 or self.debug_mask is None or depth01 is None:
            return
        x, y, w, h = self.blob
        d = cv2.resize(depth01, (self.debug_mask.shape[1], self.debug_mask.shape[0]), interpolation=cv2.INTER_LINEAR)
        sel = d[y:y + h, x:x + w][self.debug_mask[y:y + h, x:x + w] > 0]
        if sel.size < 30:
            return
        med = float(np.median(sel))
        consistent = float(np.mean(np.abs(sel - med) <= DEPTH_BAND))
        steady = 1.0 if self.depth_ref is None else 1.0 / (1.0 + (abs(med - self.depth_ref) / DEPTH_JUMP) ** 4)
        score = consistent * steady
        self.confidence = round(self.confidence * (0.5 + 0.5 * score), 3)
        self.conf_parts.update(depth=round(score, 2), depth_consistent=round(consistent, 2), depth_steady=round(steady, 2))
        if self.depth_ref is None or self.confidence >= 0.6:      # learn the target's depth only from matches we trust
            self.depth_ref = med if self.depth_ref is None else 0.8 * self.depth_ref + 0.2 * med


class LastSeen:
    """Perception status (not the mission steps): SEARCHING (never seen), TRACKING (seen now), OCCLUDED (just
    vanished, a position estimate is offered), REACQUIRING (gone a while, still looking where it was) and LOST
    (gone so long the estimate is no longer reliable).

    Remembers where the target was last confirmed and how fast it was moving, so that after it disappears
    (hidden behind something, out of the picture) we can show a ghost of the last confirmed position and a separate,
    clearly labelled guess of where it probably is now. The guess is a straight-line extrapolation and is only
    offered for PREDICT_S seconds; position confidence fades with time lost."""
    PREDICT_S = 1.5        # stop extrapolating after this long
    FADE_S = 2.5           # position confidence falls to 37% after this long
    GHOST_S = 5.0          # the ghost box is drawn this long

    def __init__(self):
        self.reset()

    def reset(self):
        self.box = None            # (cx, cy, w, h) of the last confirmed sighting
        self.t = 0.0
        self.v = (0.0, 0.0)        # centre velocity, pixels per second
        self.conf = 0.0            # tracker confidence at that sighting

    def seen(self, now, cx, cy, w, h, conf):
        if self.box is not None and 0 < now - self.t < 0.5:
            dt = now - self.t
            vx, vy = (cx - self.box[0]) / dt, (cy - self.box[1]) / dt
            self.v = (0.6 * self.v[0] + 0.4 * vx, 0.6 * self.v[1] + 0.4 * vy)
        else:
            self.v = (0.0, 0.0)
        self.box, self.t, self.conf = (cx, cy, w, h), now, conf

    def lost(self, now, frame_wh):
        """None until something has been seen; otherwise a dict describing what is known about the missing target."""
        if self.box is None:
            return None
        age = max(0.0, now - self.t)
        cx, cy, w, h = self.box
        W, H = frame_wh
        pred = None
        if age <= self.PREDICT_S and (abs(self.v[0]) + abs(self.v[1])) > 25:
            pred = (min(max(cx + self.v[0] * age, 0), W), min(max(cy + self.v[1] * age, 0), H))
        col = "left" if cx < W / 3 else "right" if cx > 2 * W / 3 else "centre"
        row = "upper" if cy < H / 3 else "lower" if cy > 2 * H / 3 else "middle"
        where = "centre" if (col, row) == ("centre", "middle") else f"{row} {col}".replace("upper centre", "top centre").replace("lower centre", "bottom centre").replace("middle ", "")
        heading = "left" if self.v[0] < -30 else "right" if self.v[0] > 30 else None
        status = "OCCLUDED" if age <= self.PREDICT_S else "REACQUIRING" if age <= self.GHOST_S else "LOST"
        return {"status": status, "ago": round(age, 1), "last": [round(cx), round(cy), round(w), round(h)],
                "pred": [round(pred[0]), round(pred[1])] if pred else None, "where": where, "heading": heading,
                "pos_conf": 0.0 if status == "LOST" else round(self.conf * float(np.exp(-age / self.FADE_S)), 2)}
