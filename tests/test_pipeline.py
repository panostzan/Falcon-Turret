"""End-to-end test with no hardware: synthetic camera frames -> flip -> color tracker -> aiming -> async
gimbal -> simulated ESP. Run: python tests/test_pipeline.py"""
import os
import sys
import time

HERE = os.path.dirname(__file__)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "laptop"))
import cv2           # noqa: E402
import numpy as np   # noqa: E402
import app           # noqa: E402  (importing starts nothing: the server only runs under __main__)
from aim import Aimer           # noqa: E402
from gimbal import AsyncGimbal  # noqa: E402
from test_gimbal import FakeESP  # noqa: E402


def frame_with_box(cx, cy):
    f = np.full((480, 640, 3), 90, np.uint8)
    cv2.rectangle(f, (0, 0), (200, 300), (225, 225, 225), -1)                 # white wall
    cv2.rectangle(f, (cx - 45, cy - 35), (cx + 45, cy + 35), (128, 128, 0), -1)   # teal box (BGR)
    cv2.rectangle(f, (cx - 45, cy - 5), (cx + 45, cy + 5), (250, 250, 250), -1)   # glare strip across it
    return cv2.imencode(".jpg", f)[1].tobytes()


def run(cx, cy, mirror=True, frames=25):
    esp = FakeESP()
    gimbal = AsyncGimbal(link=esp)
    app.tracker.use_preset("teal")
    app.predictor.reset()
    app.ctl.update(aimer=Aimer(gimbal), tracking=True, error=None)
    app.ctl["aimer"].sign.update(app.DEFAULT_SIGNS)
    app.view.update(mirror=mirror, vflip=False, mask=False)
    jpg, times = frame_with_box(cx, cy), []
    for _ in range(frames):
        t = time.perf_counter()
        out, found = app.process(jpg)
        times.append((time.perf_counter() - t) * 1000)
        assert found, f"target lost at ({cx},{cy})"
    time.sleep(0.4)                                   # let the background sender finish
    gimbal.close()
    return esp.pan, esp.tilt, sum(times[5:]) / len(times[5:]), out


def run_moving(lead_ms, frames=40):
    """A teal box sliding right at 150 px/s (15 fps); returns the final pan and the box's last x."""
    esp = FakeESP()
    gimbal = AsyncGimbal(link=esp)
    app.tracker.use_preset("teal")
    app.predictor.reset()
    app.ctl.update(aimer=Aimer(gimbal), tracking=True, error=None)
    app.ctl["aimer"].sign.update(app.DEFAULT_SIGNS)
    app.view.update(mirror=False, vflip=False, mask=False)
    app.cfg["lead_ms"] = lead_ms
    for i in range(frames):
        app.process(frame_with_box(150 + 10 * i, 240), i / 15)     # frame i is stamped i/15 s
    time.sleep(0.4)
    gimbal.close()
    return esp.pan, 150 + 10 * (frames - 1)


def test_moving():
    pan_plain, box_x = run_moving(0)
    pan_lead, _ = run_moving(250)
    app.cfg["lead_ms"] = 250
    plain_x = 320 + (pan_plain - 90) / 90 * 320            # where the aim was pointing, in pixels
    lead_x = 320 + (pan_lead - 90) / 90 * 320
    assert abs(plain_x - box_x) < 45, f"without lead the aim should be near the box ({plain_x:.0f} vs {box_x})"
    assert lead_x > plain_x + 20, f"with lead the aim should be ahead of the box: {lead_x:.0f} vs {plain_x:.0f}"
    assert lead_x < plain_x + 60, f"the lead should be modest (150 px/s * 0.25 s = 38 px), got {lead_x - plain_x:.0f}"
    print(f"moving target: aim {plain_x:.0f} px without lead, {lead_x:.0f} px with lead (box at {box_x})")


def test():
    test_moving()
    # mirrored picture: a box at source x=480 appears at x=160 (left of center) -> pan below 90
    pan, tilt, ms, _ = run(480, 360)
    assert abs(pan - 45) <= 3 and abs(tilt - 112) <= 3, (pan, tilt)
    # box on the other side
    pan, tilt, ms2, _ = run(160, 120)
    assert abs(pan - 135) <= 3 and abs(tilt - 158) <= 3, (pan, tilt)
    # no mirror: x maps straight through
    pan, tilt, _, _ = run(480, 240, mirror=False)
    assert abs(pan - 135) <= 3 and abs(tilt - 135) <= 3, (pan, tilt)
    # centered box -> straight ahead
    pan, tilt, _, _ = run(320, 240, mirror=False)
    assert abs(pan - 90) <= 2 and abs(tilt - 135) <= 2, (pan, tilt)
    # the color-match view tints the accepted pixels
    app.view.update(mask=True)
    app.tracker.use_preset("teal")
    out, _ = app.process(frame_with_box(320, 240))
    img = cv2.imdecode(np.frombuffer(out, np.uint8), 1)
    b, g, r = (int(v) for v in img[220, 300])            # a teal part of the box (the glare strip is at y=240)
    assert b > 150 and r > 120 and g < 120, f"mask tint missing, pixel is BGR ({b},{g},{r})"
    app.view["mask"] = False
    assert max(ms, ms2) < 40, f"processing takes {max(ms, ms2):.0f} ms per frame, too slow"
    print(f"pipeline tests passed (decode + flip + track + aim + encode: {max(ms, ms2):.1f} ms per frame)")


if __name__ == "__main__":
    test()
