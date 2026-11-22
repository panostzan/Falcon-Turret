"""Smooth servo motion: instead of jumping to the goal angle, an axis speeds up, cruises and brakes so it arrives exactly on the goal
without overshooting (a trapezoid speed profile). Two numbers shape it: the top speed (degrees per second) and the acceleration
(degrees per second per second). Pure arithmetic, no hardware: the tests simulate it.
"""
import math

DEFAULT_VMAX = 320.0      # deg/s: about half a second from one end of the pan range to the other
DEFAULT_ACCEL = 1800.0    # deg/s^2: reaches top speed in about 0.18 s


def step(pos, vel, goal, dt, vmax=DEFAULT_VMAX, accel=DEFAULT_ACCEL):
    """Advance one axis by dt seconds toward `goal`. Returns (new position, new velocity).

    The fastest speed that still lets the axis stop on the goal is sqrt(2 * accel * distance), so it brakes in time instead of
    overshooting; the speed changes by at most accel * dt per step, which is the easing."""
    err = goal - pos
    if abs(err) < 1e-6 and abs(vel) < 1e-6:
        return goal, 0.0
    brake = math.sqrt(2.0 * accel * abs(err))
    want = math.copysign(min(vmax, brake), err)
    dv = max(-accel * dt, min(accel * dt, want - vel))
    vel += dv
    new = pos + vel * dt
    if (goal - new) * err <= 0 or (abs(goal - new) < 0.25 and abs(vel) < accel * dt * 2):   # arrived (or about to cross): stop on the goal
        return goal, 0.0
    return new, vel
