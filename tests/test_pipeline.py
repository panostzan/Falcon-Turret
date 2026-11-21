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


def test():
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
