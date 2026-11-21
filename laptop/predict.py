"""Motion prediction ("lead") for a moving target.

The picture reaches the aiming code a few hundred milliseconds after the scene happened (camera, Pi, Wi-Fi,
servo travel), so a moving target is always somewhere further along than the frame shows. This keeps a
smoothed position and velocity for the target (an alpha-beta filter, a light cousin of a Kalman filter that
copes with uneven frame timing) and aims at where the target will be `lead_s` seconds from the frame's time.
"""


class LeadPredictor:
    """Feed it each measured target position; it returns the point to aim at (same units, e.g. pixels).

    alpha, beta   filter gains: how strongly each new measurement corrects the position / the velocity.
    v_min         speeds below this (units per second) count as standing still, so measurement noise is not
                  multiplied by the lead into jitter.
    max_lead_frac the lead never exceeds this share of the frame, so a bad velocity estimate cannot fling the
                  aim point across the picture.
    reset_gap     after this many seconds without a measurement the filter forgets (target was lost).
    """

    def __init__(self, alpha=0.6, beta=0.35, v_min=60.0, max_lead_frac=0.3, reset_gap=0.5):
        self.alpha, self.beta = alpha, beta
        self.v_min, self.max_lead_frac, self.reset_gap = v_min, max_lead_frac, reset_gap
        self.reset()

    def reset(self):
        self._t = None
        self._x = [0.0, 0.0]
        self._v = [0.0, 0.0]

    @property
    def velocity(self):
        return tuple(self._v)

    def update(self, ts, x, y, lead_s, size):
        """ts: measurement time (seconds). size: (width, height) bounds. Returns the (x, y) to aim at."""
        if self._t is None or ts - self._t > self.reset_gap or ts <= self._t:
            self._x, self._v, self._t = [float(x), float(y)], [0.0, 0.0], ts
            return (float(x), float(y))
        dt = ts - self._t
        for i, z in enumerate((x, y)):
            predicted = self._x[i] + self._v[i] * dt
            residual = z - predicted
            self._x[i] = predicted + self.alpha * residual
            self._v[i] += self.beta * residual / dt
        self._t = ts
        aim = []
        for i, limit in enumerate(size):
            v = self._v[i] if abs(self._v[i]) >= self.v_min else 0.0
            cap = self.max_lead_frac * limit
            lead = max(-cap, min(cap, v * lead_s))
            aim.append(min(max(self._x[i] + lead, 0.0), float(limit)))
        return tuple(aim)
