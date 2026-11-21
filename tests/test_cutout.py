"""Tests the Cutout layer (dim everything farther than the target). Run: python tests/test_cutout.py"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "laptop"))
import numpy as np   # noqa: E402
import app   # noqa: E402


def test():
    frame = np.full((480, 640, 3), 200, np.uint8)
    dmap = np.zeros((224, 308), np.float32)
    dmap[:, :154] = 0.2                                  # left half: far
    dmap[:, 154:] = 0.8                                  # right half: near, same depth as the target
    app.view["cutout"] = True
    app.depth_state["stale"] = None
    app.cfg["cut"] = 15

    app.tracker.clear()                                  # no target: nothing is dimmed (and it says it is not active)
    assert not app.cutout_active() and (app.apply_cutout(frame, dmap) == frame).all()

    app.tracker.use_preset("teal")
    app.tracker.depth_focus, app.tracker.missed = 0.8, 0
    assert app.cutout_active()
    out = app.apply_cutout(frame, dmap)
    far_side, near_side = out[:, :280].mean(), out[:, 360:].mean()
    assert far_side < 80 and near_side > 195, (far_side, near_side)      # far part dimmed to ~25%, target depth untouched

    app.tracker.depth_focus = 0.2                        # target is far: the near half is NOT dimmed (only farther things are)
    out = app.apply_cutout(frame, dmap)
    assert out.mean() > 195, out.mean()

    app.tracker.depth_focus = 0.8
    app.cfg["cut"] = 60                                  # a wide reach keeps more: 0.2 is within 0.6 of 0.8 -> bright
    assert app.apply_cutout(frame, dmap).mean() > 195

    app.cfg["cut"] = 15
    stale = np.ones((60, 80), np.float32)                # the whole picture moved since the depth: nothing to trust, nothing dimmed
    app.depth_state["stale"] = stale
    assert app.apply_cutout(frame, dmap).mean() > 195
    app.depth_state["stale"] = None

    app.tracker.missed = 11                              # target lost for a while: stop dimming rather than use an old depth
    assert not app.cutout_active() and (app.apply_cutout(frame, dmap) == frame).all()
    assert (app.apply_cutout(frame, None) == frame).all()    # no depth map yet: harmless
    app.view["cutout"] = False
    app.tracker.clear()
    print("cutout tests passed")


if __name__ == "__main__":
    test()
