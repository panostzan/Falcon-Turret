"""The roof and the platform: Dormant / Active, plus a manual Hibernate.

    DORMANT    roof closed, platform down, pan/tilt parked. Watching for the target. Nothing else happens.
    OPENING    roof opening, then (a moment later) the platform rising.
    ACTIVE     roof open, platform up: the turret may aim (and, if a person has armed it, fire).
    CLOSING    platform going down, then (a moment later) the roof closing.
    HIBERNATE  roof closed and nothing happens at all (no waking, no aiming, no arming) until a person switches back to Dormant.

Order, so the platform never meets the roof: opening starts the ROOF first and the platform STAGGER_S later; closing starts the
PLATFORM first and the roof STAGGER_S later. Both servos may move at the same time (the stagger is short).

The roof wakes when the target is seen (with enough confidence) for WAKE_S, and closes after SLEEP_AFTER_S without it. Waking never
arms anything: "armed" stays a human decision (see engage.py).

The class does no timing or hardware of its own: the caller passes the time into every call and hands in `move(lift=..., aux=...)`,
so the tests can drive it with a fake clock.
"""
import os
import threading

DORMANT, OPENING, ACTIVE, CLOSING, HIBERNATE = "DORMANT", "OPENING", "ACTIVE", "CLOSING", "HIBERNATE"

ROOF_CLOSED, ROOF_OPEN = 140, 20       # the roof is the aux servo (PCA9685 channel 3): 140 = closed, 20 = open (it is not opened further than this;
                                       # the driver enforces the same limit, see gimbal.AUX_RANGE)
LIFT_DOWN, LIFT_UP = 0, 150            # the platform is the lift servo (channel 2): 0 = down, 150 = up, its highest point (only once the roof is open)
TRAVEL_S = 0.5                         # how long the platform takes to get there (it is sent straight to its angle)
ROOF_MOVE_S = float(os.environ.get("TURRET_ROOF_SECONDS", 1.4))   # the roof is walked to its angle over this long (gentler on the servo),
                                                                  # with a soft start and stop. Raise it to go slower.
STAGGER_S = float(os.environ.get("TURRET_ROOF_STAGGER", 0.25))   # delay between the two servos
WAKE_S = 0.3                           # the target must be seen this long (with enough confidence) to open the roof
SLEEP_AFTER_S = 10.0                   # this long without the target and the roof closes
BLINK_S = 0.3                          # a gap in sighting shorter than this does not break the run (one missed frame)


class Roof:
    def __init__(self, move, on_close=None, hibernating=False):
        """move(lift=None, aux=None) commands the servos. on_close() is called once each time a close begins (the app disarms
        and parks pan/tilt there)."""
        self.move, self.on_close = move, on_close
        self._lock = threading.RLock()   # the roof is used from the roof loop, the aim loop, the video loop and the web handler
        self.hibernating = bool(hibernating)
        self.state = DORMANT
        self._plan = []                  # [(time, {"lift": ..} or {"aux": ..})] still to send
        self._aux = None                 # the roof angle last sent (None = not known yet: the first move is a straight jump)
        self._glide = None               # (from, to, start time, seconds) while the roof is being walked to its angle
        self._done_at = None             # when the current opening/closing is finished
        self._run_start = None           # when the current run of sightings began
        self._last_seen = None

    # ---- what the rest of the program asks ----
    @property
    def can_aim(self):
        """True only when the roof is fully open and the platform is up."""
        return self.state == ACTIVE

    @property
    def closed(self):
        """True when everything has been sent and the roof is at rest (dormant or hibernating)."""
        return self.state in (DORMANT, HIBERNATE) and not self._plan and self._glide is None

    def info(self, now):
        with self._lock:
            return self._info(now)

    def _info(self, now):
        sleep_in = None
        if self.state == ACTIVE and self._last_seen is not None:
            sleep_in = max(0.0, SLEEP_AFTER_S - (now - self._last_seen))
        return {"state": self.state, "hibernate": self.hibernating, "sleep_in": None if sleep_in is None else round(sleep_in, 1)}

    # ---- commands ----
    def start(self, now):
        with self._lock:
            return self._start(now)

    def _start(self, now):
        """Tracking has just started and the position of the roof is not known: put everything at rest (platform down, then roof)."""
        self._begin_close(now)

    def shutdown(self, now):
        with self._lock:
            return self._shutdown(now)

    def _shutdown(self, now):
        """Tracking is being switched off: close up. (Hibernation is left as it is.)"""
        if self.state != CLOSING and not self.closed:
            self._begin_close(now)

    def set_hibernate(self, on, now):
        with self._lock:
            return self._set_hibernate(on, now)

    def _set_hibernate(self, on, now):
        self.hibernating = bool(on)
        if on:
            if self.state in (ACTIVE, OPENING):
                self._begin_close(now)               # it finishes in HIBERNATE
            elif self.state == DORMANT:
                self.state = HIBERNATE
        elif self.state == HIBERNATE:
            self.state = DORMANT
            self._run_start = self._last_seen = None  # a target still in view must be seen afresh before the roof opens

    def observe(self, now, seen):
        with self._lock:
            return self._observe(now, seen)

    def _observe(self, now, seen):
        """Tell the roof whether the target is in view with enough confidence right now. Safe to call from several places."""
        if seen:
            if self._run_start is None or (self._last_seen is not None and now - self._last_seen > BLINK_S):
                self._run_start = now
            self._last_seen = now
        elif self._run_start is not None and self._last_seen is not None and now - self._last_seen > BLINK_S:
            self._run_start = None
        if (self.state == DORMANT and not self.hibernating and self._run_start is not None
                and self._last_seen - self._run_start >= WAKE_S):
            self._begin_open(now)

    def update(self, now):
        with self._lock:
            return self._update(now)

    def _update(self, now):
        """Call often (about every 20 ms): sends what is due and finishes an opening or closing."""
        while self._plan and self._plan[0][0] <= now:
            self._send(self._plan[0][1], now)
            self._plan.pop(0)                         # only after a successful send: a failed one is tried again next time
        self._step_glide(now)
        if self._done_at is not None and now >= self._done_at and not self._plan and self._glide is None:
            self._done_at = None
            if self.state == OPENING:
                self.state = ACTIVE
                if self._last_seen is None:
                    self._last_seen = now
            elif self.state == CLOSING:
                self.state = HIBERNATE if self.hibernating else DORMANT
                self._run_start = self._last_seen = None
        if self.state == ACTIVE and self._last_seen is not None and now - self._last_seen >= SLEEP_AFTER_S:
            self._begin_close(now)

    # ---- sending: the platform goes straight to its angle, the roof is walked there ----
    def _send(self, cmd, now):
        if "aux" in cmd and self._aux is not None and cmd["aux"] != self._aux:
            self._glide = (self._aux, cmd["aux"], now, max(0.05, ROOF_MOVE_S))      # starts from wherever it last was, even mid-move
        else:
            self.move(**cmd)
            if "aux" in cmd:
                self._aux = cmd["aux"]

    def _step_glide(self, now):
        """One small step of the roof's walk (called every tick). A smoothstep curve: slow start, quicker middle, slow finish."""
        if self._glide is None:
            return
        a, b, t0, dur = self._glide
        p = min(1.0, max(0.0, (now - t0) / dur))
        angle = b if p >= 1.0 else int(round(a + (b - a) * p * p * (3 - 2 * p)))
        if angle != self._aux:
            self.move(aux=angle)
            self._aux = angle
        if p >= 1.0:
            self._glide = None

    # ---- the two sequences ----
    def _begin_open(self, now):
        self.state = OPENING
        self._glide = None
        self._plan = [(now, {"aux": ROOF_OPEN}), (now + STAGGER_S, {"lift": LIFT_UP})]    # roof first, platform after
        self._done_at = now + max(STAGGER_S + TRAVEL_S, ROOF_MOVE_S)

    def _begin_close(self, now):
        self.state = CLOSING
        if self.on_close:
            self.on_close()
        self._glide = None
        self._plan = [(now, {"lift": LIFT_DOWN}), (now + STAGGER_S, {"aux": ROOF_CLOSED})]  # platform first, roof after
        self._done_at = now + STAGGER_S + max(TRAVEL_S, ROOF_MOVE_S)
