"""Tests the depth smoother with synthetic depth maps (no network needed): glide, flicker removal, motion compensation.
Run: python tests/test_depth_smooth.py"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "laptop"))
import cv2   # noqa: E402
import numpy as np   # noqa: E402
import depth_nn   # noqa: E402

rng = np.random.default_rng(1)
W, H = 308, 224


def scene_map(shift=0.0):
    d = np.zeros((H, W), np.float32)
    cv2.circle(d, (int(150 + shift), 110), 50, 1.0, -1)
    return cv2.GaussianBlur(d, (0, 0), 6)


def scene_thumb(shift=0.0):
    img = np.full((60, 80), 40, np.float32)
    cv2.circle(img, (int((150 + shift) * 80 / W), 28), 12, 200, -1)
    cv2.putText(img, "TX", (10, 50), cv2.FONT_HERSHEY_SIMPLEX, 0.5, 255, 1)       # texture so correlation has something to lock to
    return img


def test():
    # 1. glide: after a jump from map A to map B the display moves in small steps, never all at once
    sm = depth_nn.DepthSmoother()
    a, b = np.zeros((H, W), np.float32), np.ones((H, W), np.float32)
    th = scene_thumb()
    sm.update(a, 1.0, th, 10.0)
    sm.update(b, 1.15, th, 10.15)
    levels = [float(sm.current(10.15 + i * 0.01, th).mean()) for i in range(0, 25)]
    steps = np.diff(levels)
    assert levels[0] < 0.1, "fade must start from what was on screen"
    assert (steps >= -1e-6).all() and steps.max() < 0.2, f"jumped: {steps.max():.2f}"
    assert levels[-1] > 0.4, "it must actually arrive"      # target is an average with the older map, so < 1 is expected

    # 2. flicker: noisy maps every 0.15 s, shown at 15 fps. Smoothed output varies much less from frame to frame
    def run(smooth):
        sm, th, prev, diffs, t = depth_nn.DepthSmoother(), scene_thumb(), None, [], 0.0
        base = scene_map()
        shown = None
        for k in range(40):
            if k % 2 == 0:
                raw = np.clip(base + rng.normal(0, 0.12, base.shape).astype(np.float32), 0, 1)
                sm.update(raw, t, th, t)
                latest = raw
            cur = sm.current(t, th) if smooth else latest
            if prev is not None:
                diffs.append(float(np.abs(cur - prev).mean()))
            prev, t = cur, t + 0.075
        return float(np.mean(diffs[4:]))
    raw_flicker, smooth_flicker = run(False), run(True)
    assert smooth_flicker < 0.6 * raw_flicker, (raw_flicker, smooth_flicker)

    # 3. motion compensation: the camera pans 8 thumbnail px after the map was computed; the map follows the scene
    sm = depth_nn.DepthSmoother()
    sm.update(scene_map(0), 5.0, scene_thumb(0), 5.0)
    moved = sm.current(5.5, scene_thumb(8 * W / 80))          # scene moved 8 thumb px = ~31 depth px
    ys, xs = np.nonzero(moved > 0.5)
    base_xs = np.nonzero(scene_map(0) > 0.5)[1]
    assert abs(xs.mean() - (base_xs.mean() + 8 * W / 80)) < 6, (xs.mean(), base_xs.mean())
    assert abs(xs.mean() - base_xs.mean()) > 20, "the map did not move with the scene"
    still = sm.current(5.5, scene_thumb(0))
    assert np.allclose(still, scene_map(0), atol=1e-4), "no movement must mean no shift"
    huge = sm.current(5.5, scene_thumb(60 * W / 80))           # an untrustworthy jump is ignored, not followed
    assert np.allclose(huge, scene_map(0), atol=1e-4)

    # 4. the same result fed twice is ignored; reset clears
    sm.update(scene_map(0), 5.0, scene_thumb(0), 6.0)
    sm.reset()
    assert sm.current(6.0) is None
    print(f"depth smoother tests passed (frame-to-frame flicker {raw_flicker:.3f} -> {smooth_flicker:.3f})")


if __name__ == "__main__":
    test()
