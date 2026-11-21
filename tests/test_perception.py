"""Integration test: the full processing step through SEARCHING, TRACKING, OCCLUDED, REACQUIRING, LOST, with the ghost
drawn on the picture. No hardware. Run: python tests/test_perception.py"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "laptop"))
import cv2   # noqa: E402
import numpy as np   # noqa: E402
import app   # noqa: E402


def jpg(cx=None):
    f = np.full((480, 640, 3), 90, np.uint8)
    if cx is not None:
        cv2.rectangle(f, (cx - 40, 200), (cx + 40, 270), (128, 128, 0), -1)      # teal box
    return cv2.imencode(".jpg", f)[1].tobytes()


def status(found):
    lost = app.info.get("lost")
    return "TRACKING" if found else (lost or {}).get("status", "SEARCHING")


def test():
    app.view.update(mirror=False, vflip=False, mask=False, depth=False, box=True, trail=False)
    app.ctl.update(tracking=False, aimer=None, error=None)
    app.tracker.use_preset("teal")
    app.last_seen.reset()
    t = 100.0
    _, found = app.process(jpg(None), t)
    assert status(found) == "SEARCHING"                                     # armed, nothing ever seen

    for i in range(12):                                                     # a target moving right
        t += 0.066
        _, found = app.process(jpg(150 + 12 * i), t)
        assert found
    assert status(found) == "TRACKING" and app.info["lost"] is None
    seen_at = t

    t = seen_at + 0.6                                                       # vanishes
    out, found = app.process(jpg(None), t)
    assert status(found) == "OCCLUDED", app.info["lost"]
    assert app.info["lost"]["pred"] is not None
    img = cv2.imdecode(np.frombuffer(out, np.uint8), 1)
    lx = app.info["lost"]["last"][0]
    ghost = img[190:205, lx - 60:lx + 60].astype(int)                       # the dashed top edge of the ghost box is drawn
    assert (np.abs(ghost - 90).sum(axis=2) > 30).any(), "no ghost drawn on the picture"

    t = seen_at + 3.0
    out, found = app.process(jpg(None), t)
    assert status(found) == "REACQUIRING" and app.info["lost"]["pred"] is None
    t = seen_at + 7.0
    out, found = app.process(jpg(None), t)
    assert status(found) == "LOST" and app.info["lost"]["pos_conf"] == 0.0

    t += 0.1
    _, found = app.process(jpg(400), t)                                     # it comes back
    assert found and status(found) == "TRACKING"

    app.view["box"] = False                                                 # layer off: nothing drawn, status still reported
    app.last_seen.reset()
    app.tracker.clear()
    print("perception status tests passed")


if __name__ == "__main__":
    test()
