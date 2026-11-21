"""Tests the color tracker on synthetic frames. Run: python tests/test_tracker.py"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "laptop"))
import cv2           # noqa: E402
import numpy as np   # noqa: E402
from tracker import ColorTracker   # noqa: E402


def scene(bottle_xy, bottle_color, light=True, wall=False):
    f = np.full((480, 640, 3), 90, np.uint8)                                  # gray room
    if wall:
        cv2.rectangle(f, (0, 0), (260, 440), (225, 225, 225), -1)             # big near-white wall
    if light:
        cv2.circle(f, (590, 50), 25, (255, 255, 255), -1)                     # bright ceiling light
    cv2.rectangle(f, (bottle_xy[0] - 25, bottle_xy[1] - 60), (bottle_xy[0] + 25, bottle_xy[1] + 60), bottle_color, -1)
    return f


def follow(name, color, wall):
    t = ColorTracker()
    assert t.select(scene((330, 300), color, wall=wall), 330, 300), f"{name}: select failed"
    errs = []
    for x in range(330, 530, 20):                                          # target slides right
        r = t.find(scene((x, 300), color, wall=wall))
        assert r is not None, f"{name}: lost at x={x}"
        errs.append(abs(r[0] - x) + abs(r[1] - 300))
    for _ in range(6):                                                      # then holds still
        r = t.find(scene((530, 300), color, wall=wall))
    settled = abs(r[0] - 530) + abs(r[1] - 300)
    assert max(errs) < 25 and settled < 3, f"{name}: moving err {max(errs):.1f}px, settled err {settled:.1f}px"
    assert r[2] < 80 and r[3] < 160, f"{name}: box too big ({r[2]:.0f}x{r[3]:.0f}), bottle is 50x120"
    assert t.find(scene((300, 300), (90, 90, 90), light=False)) is None, f"{name}: should lose target"
    print(f"{name}: ok (moving err {max(errs):.1f}px, settled {settled:.1f}px, box {r[2]:.0f}x{r[3]:.0f})")


def follow_preset(name, color):
    t = ColorTracker()
    t.use_preset(name)
    bg = scene((0, 0), (90, 90, 90), light=True, wall=True)                 # white wall + ceiling light
    cv2.rectangle(bg, (400, 300), (470, 360), (60, 200, 60), -1)            # green distractor
    errs = []
    for x in range(300, 520, 20):
        f = bg.copy(); cv2.rectangle(f, (x - 30, 120), (x + 30, 200), color, -1)
        r = t.find(f)
        assert r is not None, f"{name}: lost at x={x}"
        errs.append(abs(r[0] - x) + abs(r[1] - 160))
    assert max(errs) < 25, f"{name}: error {max(errs):.1f}px"
    assert r[2] < 80 and r[3] < 100, f"{name}: box {r[2]:.0f}x{r[3]:.0f}"
    print(f"preset {name}: ok (err {max(errs):.1f}px, box {r[2]:.0f}x{r[3]:.0f})")


def fragmented_box(cx, cy, w=130, h=90):
    """A teal box with a white glare strip, a dark shadow band and a label cutting through it."""
    f = scene((0, 0), (90, 90, 90), light=True, wall=True)
    cv2.rectangle(f, (cx - w // 2, cy - h // 2), (cx + w // 2, cy + h // 2), (128, 128, 0), -1)
    cv2.rectangle(f, (cx - w // 2, cy - 22), (cx + w // 2, cy - 12), (250, 250, 250), -1)    # glare strip
    cv2.rectangle(f, (cx - w // 2, cy + 14), (cx + w // 2, cy + 26), (30, 30, 0), -1)        # shadow band
    cv2.rectangle(f, (cx - 12, cy - h // 2), (cx + 4, cy + h // 2), (200, 200, 200), -1)      # vertical label
    return f


def test_fragmented():
    for how in ("preset", "click"):
        t = ColorTracker()
        if how == "preset":
            t.use_preset("teal")
        else:
            assert t.select(fragmented_box(350, 220), 350 + 40, 220 - 35), "click on the box failed"
        for cx in range(300, 460, 20):                       # box slides right
            r = t.find(fragmented_box(cx, 220))
            assert r is not None, f"{how}: lost at x={cx}"
            assert abs(r[0] - cx) < 12 and abs(r[1] - 220) < 12, f"{how}: center off ({r[0]:.0f},{r[1]:.0f}) vs ({cx},220)"
            assert r[2] > 110 and r[3] > 75, f"{how}: box only {r[2]:.0f}x{r[3]:.0f}, the whole box is 130x90"
        print(f"fragmented teal box ({how}): ok, box {r[2]:.0f}x{r[3]:.0f}")


def test():
    test_fragmented()
    follow_preset("teal", (128, 128, 0))          # BGR of teal (0,128,128)
    follow_preset("blue", (255, 0, 0))           # BGR of pure blue
    follow("white bottle", (215, 215, 215), wall=False)
    follow("white bottle near white wall", (215, 215, 215), wall=True)
    follow("orange marker", (30, 120, 250), wall=False)
    t = ColorTracker()
    assert not t.select(scene((330, 300), (215, 215, 215), wall=True), 100, 200), "clicking the wall must be refused"
    assert not t.armed
    assert t.find(scene((100, 100), (255, 255, 255))) is None, "not armed -> nothing"
    print("all tracker tests passed")


if __name__ == "__main__":
    test()
