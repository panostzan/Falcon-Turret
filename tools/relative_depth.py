"""Live RELATIVE depth from the two C270 webcams. No calibration, no metres: warm = nearer, cool = farther, black = unsure.

Run:  python tools\\relative_depth.py            (cameras 1 and 2, as listed by the stereo module's `check` command)
      python tools\\relative_depth.py --left 2 --right 1
Keys: Q / Esc quit,  Space freeze,  W swap left/right (if near things look cool),  S snapshot,  + / - matching range.

This works for a rig that is mounted rigidly, side by side and level (vertical shift under about 2 px). It does plain
block matching (OpenCV SGBM) on the raw pictures, so it needs no alignment step. If the cameras are swapped it notices
and flips them itself. Use it on a scene with texture at 1-4 m; blank walls and very close objects give black.
"""
import argparse
import os
import sys
import time

import cv2
import numpy as np

SIZE = (480, 360)          # matching runs at this size (width, height)


def make_matcher(num_disparities=96, block=7):
    num_disparities = max(16, (num_disparities // 16) * 16)          # SGBM wants a multiple of 16
    return cv2.StereoSGBM_create(
        minDisparity=0, numDisparities=num_disparities, blockSize=block,
        P1=8 * block * block, P2=32 * block * block, uniquenessRatio=10,
        speckleWindowSize=80, speckleRange=2, disp12MaxDiff=1)


def to_gray(img):
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    return cv2.resize(g, SIZE, interpolation=cv2.INTER_AREA)


def disparity(matcher, left_gray, right_gray):
    """Disparity in pixels (float32); invalid pixels are <= 0."""
    d = matcher.compute(left_gray, right_gray).astype(np.float32) / 16.0
    return cv2.medianBlur(np.where(d > 0, d, 0).astype(np.float32), 5)


def colorize(d, num_disparities):
    """Heatmap: warm = large disparity = near. Invalid pixels are black."""
    col = cv2.applyColorMap(np.clip(255 * d / num_disparities, 0, 255).astype(np.uint8), cv2.COLORMAP_TURBO)
    col[d <= 0] = 0
    return col


def pick_order(matcher, left_gray, right_gray):
    """True if the pair is in the right order (left camera first). Features shift one way for a correct pair; swapped
    cameras make almost everything invalid, so the order with more valid pixels wins."""
    straight = (disparity(matcher, left_gray, right_gray) > 0).mean()
    swapped = (disparity(matcher, right_gray, left_gray) > 0).mean()
    return straight >= swapped, straight, swapped


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--left", type=int, default=1)
    p.add_argument("--right", type=int, default=2)
    p.add_argument("--disparities", type=int, default=96, help="matching range in pixels (more = can see closer things)")
    args = p.parse_args()

    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    from stereo_depth.windows import WindowsStereo           # only used to read the two cameras

    nd, matcher = args.disparities, make_matcher(args.disparities)
    title = "Relative depth: warm = near, cool = far, black = unsure (Q quits)"
    cv2.namedWindow(title)
    frozen, last, fps, t_prev = False, None, 0.0, time.time()
    swap, order_known, valid = False, False, 0.0
    with WindowsStereo(args.left, args.right, max_skew_s=.04) as cams:
        while True:
            if not frozen:
                left, right = cams.read(timeout=10)
                lg, rg = to_gray(left.image), to_gray(right.image)
                if not order_known:                           # decide once, on a frame that has some detail
                    in_order, a, b = pick_order(matcher, lg, rg)
                    if max(a, b) > 0.05:
                        order_known, swap = True, not in_order
                        if swap:
                            print(f"the cameras look swapped (valid {a:.0%} vs {b:.0%}): using them the other way round", flush=True)
                if swap:
                    left, right, lg, rg = right, left, rg, lg
                d = disparity(matcher, lg, rg)
                valid = (d > 0).mean()
                now = time.time()
                fps = 0.9 * fps + 0.1 / max(1e-3, now - t_prev) if fps else 1 / max(1e-3, now - t_prev)
                t_prev = now
                last = np.hstack([cv2.resize(left.image, SIZE), colorize(d, nd)])
            canvas = last.copy()
            note = "FROZEN" if frozen else f"{valid:.0%} of pixels valid | {fps:.0f} fps | range {nd}px"
            cv2.rectangle(canvas, (0, 0), (canvas.shape[1], 26), (20, 20, 20), -1)
            cv2.putText(canvas, "left camera", (8, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (230, 230, 230), 1, cv2.LINE_AA)
            cv2.putText(canvas, "relative depth (warm = near)   " + note, (SIZE[0] + 8, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                        (230, 230, 230), 1, cv2.LINE_AA)
            cv2.imshow(title, canvas)
            key = cv2.waitKey(1) & 0xFF
            if key in (27, ord("q")) or cv2.getWindowProperty(title, cv2.WND_PROP_VISIBLE) < 1:
                break
            if key == ord(" "):
                frozen = not frozen
            elif key == ord("w"):                              # manual override if the automatic left/right guess is wrong
                swap, order_known = not swap, True
                print("cameras swapped" if swap else "cameras in the given order", flush=True)
            elif key == ord("s"):
                os.makedirs("captures", exist_ok=True)
                path = os.path.join("captures", time.strftime("relative_depth_%H%M%S.png"))
                cv2.imwrite(path, last)
                print("saved", path, flush=True)
            elif key in (ord("+"), ord("=")) and nd < 192:
                nd += 16
                matcher = make_matcher(nd, 7)
            elif key in (ord("-"), ord("_")) and nd > 32:
                nd -= 16
                matcher = make_matcher(nd, 7)
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
