"""One-click system check: asks every part of the turret "are you working?" and returns a green / amber / red answer for each, with a
plain sentence on what to do about a problem. It only reads, except the optional servo wiggle, which moves each servo a few degrees
and puts it back (the person pressing the button asked for that).

`env` gives the check what it needs, so it can be tested without hardware (see tests/test_syscheck.py):
    feed()              the FrameFeed (or None)             camera()      {"state": ..., "age": ...}
    frame_age()         seconds since the last picture      qnx()         the dashboard's QNX summary
    tracking()          True while the dashboard holds the Feather
    held_gimbal()       the dashboard's gimbal, when tracking  open_gimbal()  a temporary Gimbal on the Feather's port (caller closes it)
    laptop()            {"ram_free_gb", "cpu_process_pct"}  depth()      {"on": bool, "available": bool}
"""
import time

PASS, WARN, FAIL, SKIP = "pass", "warn", "fail", "skip"


def _row(name, state, detail, fix=""):
    return {"name": name, "state": state, "detail": detail, "fix": fix}


def check_link(env):
    f = env["feed"]()
    if f is None:
        return _row("Pi link", FAIL, "no connection to the Pi", "Is the Pi powered and on the hotspot? Wait a minute after a power-up.")
    r = (f.rtt() or {}).get("udp") or {}
    if not r.get("n") or r.get("p50") is None:
        return _row("Pi link", WARN, "connected, but the fast control channel has not answered yet", "Wait 10 seconds and run the check again.")
    if r["p95"] > 80:
        return _row("Pi link", WARN, f"control messages take {r['p50']} ms (95th percentile {r['p95']} ms)", "The Wi-Fi is congested: move the Pi closer, or turn off other Wi-Fi use.")
    return _row("Pi link", PASS, f"control round trip {r['p50']} ms (95th percentile {r['p95']} ms)")


def check_camera(env):
    cam, age = env["camera"](), env["frame_age"]()
    if age is not None and age < 1.5:
        return _row("Camera", PASS, f"pictures arriving ({cam.get('state', 'ok')})")
    if cam.get("state") == "stalled":
        if cam.get("usb") is False:
            return _row("Camera", FAIL, "the Pi does not see the camera on USB", "Check the camera's USB cable, or power-cycle the Pi (a weak supply drops the camera).")
        return _row("Camera", FAIL, "the camera is plugged in but sends nothing", "Wait 15 seconds (it restarts itself); if it stays silent, unplug and replug the camera.")
    return _row("Camera", FAIL, "no pictures", "Check that the Pi is on and its camera is plugged in.")


def check_tracker(env):
    q = env["qnx"]()
    if not q.get("present"):
        return _row("QNX tracker", FAIL, "the tracker on the Pi is not answering", "Restart the dashboard server; if it persists, power-cycle the Pi.")
    if not q.get("link"):
        return _row("QNX tracker", WARN, "running, but its safety watchdog has disarmed it (laptop heartbeat lost)", "It re-arms by itself when the link returns; press Arm again.")
    return _row("QNX tracker", PASS, f"answering, {q.get('proc_ms')} ms per picture, mission state {q.get('state')}")


def check_interlock(env):
    q = env["qnx"]()
    if not q.get("present"):
        return _row("Fire interlock", SKIP, "needs the QNX tracker")
    if q.get("armed"):
        return _row("Fire interlock", WARN, "the turret is ARMED", "Disarm before handling the hardware.")
    return _row("Fire interlock", PASS, "disarmed (safe); the watchdog is watching the laptop link")


def check_feather(env):
    """Is the Feather answering? Uses the dashboard's own connection while tracking, otherwise opens the port briefly."""
    try:
        if env["tracking"]():
            g = env["held_gimbal"]()
            pan, tilt = g.status()
            return _row("Feather and servo driver", PASS, f"answering through the dashboard (pan {pan}, tilt {tilt})")
        g = env["open_gimbal"]()
        try:
            pan, tilt = g.status()
        finally:
            g.close()
        return _row("Feather and servo driver", PASS, f"answering on its USB port (pan {pan}, tilt {tilt})")
    except Exception as e:
        text = str(e)
        if "Access is denied" in text or "PermissionError" in text:
            return _row("Feather and servo driver", WARN, "its port is held by another program", "Close the servo sliders window (only one program can use the Feather).")
        if "could not open" in text or "FileNotFound" in text:
            return _row("Feather and servo driver", FAIL, "the Feather is not on its USB port", "Check the micro-USB data cable and the port name (COM8).")
        return _row("Feather and servo driver", FAIL, f"no proper answer: {text[:90]}", "Unplug and replug the Feather's USB cable.")


def check_wiggle(env, wiggle):
    if not wiggle:
        return _row("Servos", SKIP, "not run (servo actuation disabled)")
    if env["tracking"]():
        return _row("Servos", SKIP, "tracking is on", "Switch tracking off first so the check can move them.")
    try:
        g = env["open_gimbal"]()
    except Exception as e:
        return _row("Servos", FAIL, f"cannot reach the Feather: {str(e)[:80]}", "Close any program holding COM8.")
    try:
        g.center()
        time.sleep(0.6)
        for axis, delta in (("pan", 6), ("tilt", 4), ("lift", 6)):
            base = getattr(g, axis)
            for target in (base + delta, base - delta, base):
                g.move_to(**{axis: target})
                time.sleep(0.25)
        return _row("Servos", PASS, "pan, tilt, lift actuated and returned; commands acknowledged (confirm motion by eye)",
                    "If one did not move: check that channel's servo plug and the servo power supply.")
    except Exception as e:
        return _row("Servos", FAIL, f"a move failed: {str(e)[:80]}", "Check the Feather's USB cable.")
    finally:
        g.close()


def check_laptop(env):
    s = env["laptop"]()
    free = s.get("ram_free_gb")
    if free is not None and free < 1.0:
        return _row("This computer", WARN, f"only {free} GB of memory free", "Close other programs.")
    d = env["depth"]()
    return _row("This computer", PASS, "memory and CPU fine" + (", depth network " + ("loaded" if d.get("available") else "not loaded") if d.get("on") else ""))


def run(env, wiggle=False):
    """Run every check; returns {"rows": [...], "summary": {...}, "seconds": s}. A check that raises becomes a red row, never a crash."""
    t0, rows = time.time(), []
    for fn in (check_link, check_camera, check_tracker, check_interlock, check_feather, lambda e: check_wiggle(e, wiggle), check_laptop):
        try:
            rows.append(fn(env))
        except Exception as e:
            rows.append(_row(getattr(fn, "__name__", "check"), FAIL, f"the check itself failed: {e}"))
    counts = {k: sum(1 for r in rows if r["state"] == k) for k in (PASS, WARN, FAIL, SKIP)}
    verdict = FAIL if counts[FAIL] else WARN if counts[WARN] else PASS
    return {"rows": rows, "summary": dict(counts, verdict=verdict), "seconds": round(time.time() - t0, 1)}
