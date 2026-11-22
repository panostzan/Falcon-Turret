"""Autonomous engagement logic: SEARCH -> ACQUIRE -> TRACK -> LOCK -> (fire) -> COOLDOWN, with safety interlocks.

It decides only WHEN to fire; the aiming itself is aim.py. Interlocks, all of which must hold to fire:
  * the master ARM switch is on (it starts off, and arming resets the shot counter)
  * tracking is on and a target has been seen continuously
  * the gimbal has settled on the target for `lock_frames` frames, and the target is slow enough
  * the tracker's confidence stays at or above `min_confidence` the whole time (a doubtful target never locks)
  * the lock has been held for `dwell_s` seconds
  * the cooldown since the last shot has passed, and fewer than `max_shots` have been fired since arming
Firing goes through an emitter object; the default one is simulated (it only logs), because the real IR
emitter does not exist yet.
"""
from collections import deque

STATES = ("IDLE", "SEARCH", "ACQUIRE", "TRACK", "LOCK", "COOLDOWN")


class SimulatedEmitter:
    """Stand-in for the IR emitter: records shots, sends nothing anywhere."""

    def __init__(self):
        self.shots = []

    def fire(self, ts):
        self.shots.append(ts)


class Engagement:
    def __init__(self, emitter=None, acquire_frames=3, lock_frames=8, dwell_s=0.5, cooldown_s=1.5, lost_s=1.0,
                 max_speed=300.0, max_shots=30, min_confidence=0.8, scan_range=(50.0, 130.0), scan_speed=25.0, scan_tilt=135.0):
        self.emitter = emitter or SimulatedEmitter()
        self.acquire_frames, self.lock_frames = acquire_frames, lock_frames
        self.dwell_s, self.cooldown_s, self.lost_s = dwell_s, cooldown_s, lost_s
        self.max_speed, self.max_shots = max_speed, max_shots
        self.min_confidence = min_confidence    # below this the target counts as 'not sure': no lock, no shot
        self.scan_range, self.scan_speed, self.scan_tilt = scan_range, scan_speed, scan_tilt
        self.armed = False
        self.auto_scan = False
        self.auto_fire = True                     # shoot by itself on LOCK; False = only on a manual fire()
        self.human_gate = False                   # the human gate: with it on, no shot while a human is in view or the AI check is silent
        self.human, self.human_ts, self.human_timeout_s = False, None, 2.0
        self.shots = 0
        self.events = deque(maxlen=80)            # (time, text) newest last
        self.state = "IDLE"
        self._reset()

    def _reset(self):
        self._seen = 0            # consecutive frames with the target
        self._settled = 0         # consecutive frames the gimbal was settled on it
        self._last_seen = None
        self._lock_since = None
        self._cooldown_until = 0.0
        self._scan_t0 = None

    def _to(self, state, ts, note=None):
        if state != self.state:
            self.events.append((ts, f"{self.state} -> {state}" + (f" ({note})" if note else "")))
            self.state = state

    def arm(self, on, ts):
        """Master switch. Arming also resets the shot counter; disarming drops straight out of LOCK."""
        if bool(on) != self.armed:
            self.armed = bool(on)
            self.events.append((ts, "ARMED" if on else "DISARMED"))
            if on:
                self.shots = 0

    def set_human(self, present, ts):
        """The AI's latest answer (True = a human is in view). Logs HUMAN detected / cleared when it changes."""
        present = bool(present)
        if present != self.human and self.human_ts is not None:
            self.events.append((ts, "HUMAN detected" if present else "HUMAN cleared"))
        elif present and self.human_ts is None:
            self.events.append((ts, "HUMAN detected"))
        self.human, self.human_ts = present, ts

    def human_block(self, ts):
        """Why the human gate forbids a shot right now: "human detected", "face check offline" (the AI has not reported within
        human_timeout_s), or None. Always None with the gate off."""
        if not self.human_gate:
            return None
        if self.human_ts is None or ts - self.human_ts > self.human_timeout_s:
            return "face check offline"
        return "human detected" if self.human else None

    def fire(self, ts):
        """A manual shot (the operator's click). Allowed only when armed and tracking, outside the cooldown and under max_shots;
        needs no lock. Returns None when it fired, else the reason it was refused (also logged)."""
        reason = (None if self.armed else "not armed") or ("tracking off" if self.state == "IDLE" else None)             or self.human_block(ts) or ("cooldown" if ts < self._cooldown_until else None)             or ("shot limit" if self.shots >= self.max_shots else None)
        if reason:
            self.events.append((ts, f"FIRE refused: {reason}"))
            return reason
        self.emitter.fire(ts)
        self.shots += 1
        self.events.append((ts, f"FIRE #{self.shots} (manual)"))
        self._cooldown_until = ts + self.cooldown_s
        self._lock_since = None
        self._to("COOLDOWN", ts)
        return None

    def update(self, ts, tracking, found, settled, speed=0.0, confidence=1.0):
        """Call once per frame. Returns (fired, scan_goal): fired is True on the frame a shot is taken;
        scan_goal is a (pan, tilt) to sweep to while searching with auto_scan on, else None."""
        if not tracking:
            self._to("IDLE", ts)
            self._reset()
            return False, None
        if self.state == "IDLE":
            self._to("SEARCH", ts)
        if found:
            self._seen += 1
            self._last_seen = ts
        else:
            self._seen = self._settled = 0
            if self._last_seen is None or ts - self._last_seen > self.lost_s:
                if self.state not in ("SEARCH",):
                    self._to("SEARCH", ts, "target lost")
                self._lock_since = None
        fired = False

        if self.state == "SEARCH":
            if found:
                self._to("ACQUIRE", ts)
        elif self.state == "ACQUIRE":
            if self._seen >= self.acquire_frames:
                self._to("TRACK", ts)
        elif self.state in ("TRACK", "LOCK", "COOLDOWN"):
            sure = confidence >= self.min_confidence
            self._settled = self._settled + 1 if (found and settled and sure) else 0
            if self.state == "TRACK":
                if self._settled >= self.lock_frames and speed <= self.max_speed:
                    self._lock_since = ts
                    self._to("LOCK", ts)
            elif self.state == "LOCK":
                if self._settled == 0 or speed > self.max_speed:
                    self._to("TRACK", ts, "lock broken")
                    self._lock_since = None
                elif (self.armed and self.auto_fire and self.human_block(ts) is None and self.shots < self.max_shots and self._lock_since is not None
                      and ts - self._lock_since >= self.dwell_s):
                    self.emitter.fire(ts)
                    self.shots += 1
                    fired = True
                    self.events.append((ts, f"FIRE #{self.shots}"))
                    self._cooldown_until = ts + self.cooldown_s
                    self._to("COOLDOWN", ts)
            else:   # COOLDOWN
                if ts >= self._cooldown_until:
                    locked = self._settled >= self.lock_frames and speed <= self.max_speed
                    self._lock_since = ts if locked else None
                    self._to("LOCK" if locked else "TRACK", ts)
        return fired, self._scan_goal(ts)

    def _scan_goal(self, ts):
        """While searching with auto_scan on, sweep the pan back and forth (a triangle wave)."""
        if not self.auto_scan or self.state != "SEARCH":
            self._scan_t0 = None
            return None
        if self._scan_t0 is None:
            self._scan_t0 = ts
        lo, hi = self.scan_range
        span = hi - lo
        phase = ((ts - self._scan_t0) * self.scan_speed) % (2 * span)
        return (lo + (phase if phase <= span else 2 * span - phase), self.scan_tilt)
