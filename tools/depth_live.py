"""Live neural depth from ONE of the cameras plugged into this laptop. Left: the camera. Right: estimated relative depth.

Run:  python tools\\depth_live.py --camera 1                 (a window; Q / Esc quits)
      python tools\\depth_live.py --camera 1 --frames 60 --headless --snapshot captures\\depth_live.png
Camera numbers are DirectShow indices: 0 is the built-in camera, 1 and 2 were the two C270s. Close the dashboard/other
camera programs first: only one program can use a camera at a time.
"""
import argparse
import os
import sys
import time

import cv2
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "laptop"))
from depth_nn import NeuralDepth, overlay   # noqa: E402


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--camera", type=int, default=1)
    p.add_argument("--frames", type=int, default=0, help="0 = run until closed")
    p.add_argument("--headless", action="store_true")
    p.add_argument("--snapshot", help="save the last picture to this path")
    args = p.parse_args()
    if args.headless and not args.frames:
        p.error("--headless needs --frames")

    net = NeuralDepth()
    if not net.available:
        sys.exit(net.error)
    cap = cv2.VideoCapture(args.camera, cv2.CAP_DSHOW)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    if not cap.isOpened():
        sys.exit(f"cannot open camera {args.camera}: check the number, privacy settings and other camera programs")
    title = "Neural depth (estimated, relative): Q quits"
    if not args.headless:
        cv2.namedWindow(title)
    shown, count, t0, canvas = None, 0, time.time(), None
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                sys.exit("the camera stopped delivering frames")
            net.submit(frame)
            result = net.latest()
            if result:
                shown = overlay(frame, result[0])
            canvas = np.hstack([frame, shown if shown is not None else np.zeros_like(frame)])
            note = f"camera {count / max(1e-3, time.time() - t0):.0f} fps | depth {net.fps:.1f} fps ({net.ms:.0f} ms)"
            cv2.putText(canvas, note, (10, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2, cv2.LINE_AA)
            count += 1
            if not args.headless:
                cv2.imshow(title, canvas)
                key = cv2.waitKey(1) & 0xFF
                if key in (27, ord("q")) or cv2.getWindowProperty(title, cv2.WND_PROP_VISIBLE) < 1:
                    break
            if args.frames and count >= args.frames:
                break
    finally:
        cap.release()
        net.close()
        if not args.headless:
            cv2.destroyAllWindows()
    if args.snapshot and canvas is not None:
        os.makedirs(os.path.dirname(os.path.abspath(args.snapshot)), exist_ok=True)
        cv2.imwrite(args.snapshot, canvas)
    print(f"{count} frames; depth {net.fps:.1f} fps ({net.ms:.0f} ms per picture)")


if __name__ == "__main__":
    main()
