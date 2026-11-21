"""Measures glass-to-glass latency of the camera path (camera -> Pi -> Wi-Fi -> laptop).

How: the laptop flashes its whole screen white/black. Point the camera at the screen. Latency is the time from
the flash command to the first received frame that shows the change. (The screen's own refresh adds roughly
10-20 ms, so the true figure is a touch lower.) Stop the dashboard server first: only one program can pull
frames from the Pi at a time.

Run:  python tools/latency_test.py        (press Esc in the flash window to quit early)
"""
import os
import statistics as st
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "laptop"))


def analyze(frames, events, min_contrast=15):
    """frames: [(arrival_time, brightness)]; events: [(toggle_time, to_white)].
    Returns (latencies in ms, contrast) or raises ValueError if the camera isn't seeing the flashes."""
    pairs = list(zip(events, events[1:] + [(events[-1][0] + 1.5, None)]))
    white = [b for (t0, w), (t1, _) in pairs if w for ts, b in frames if t1 - 0.5 <= ts < t1]
    black = [b for (t0, w), (t1, _) in pairs if not w for ts, b in frames if t1 - 0.5 <= ts < t1]
    if not white or not black:
        raise ValueError("no frames received while flashing")
    hi, lo = st.median(white), st.median(black)
    if hi - lo < min_contrast:
        raise ValueError(f"the camera isn't seeing the flashes (brightness {lo:.0f} vs {hi:.0f}): aim it at the screen")
    threshold = (hi + lo) / 2
    latencies = []
    for (t0, to_white), (t1, _) in pairs:
        for ts, b in frames:
            if t0 <= ts < t1 and ((b > threshold) == to_white):
                latencies.append((ts - t0) * 1000)
                break
    return latencies, hi - lo


def main(flashes=16, period=1.5):
    import cv2
    import numpy as np
    from receiver import FrameFeed

    feed, frames, stop = FrameFeed(), [], threading.Event()

    def reader():
        seq = -1
        while not stop.is_set():
            item = feed.get(seq, 0.5)
            if item:
                seq, ts, jpg = item
                gray = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_REDUCED_GRAYSCALE_4)
                frames.append((ts, float(gray.mean())))

    threading.Thread(target=reader, daemon=True).start()
    cv2.namedWindow("flash", cv2.WINDOW_NORMAL)
    cv2.setWindowProperty("flash", cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)
    imgs = {True: np.full((200, 200, 3), 255, np.uint8), False: np.zeros((200, 200, 3), np.uint8)}

    def show(white, wait):
        cv2.imshow("flash", imgs[white])
        end = time.time() + wait
        while time.time() < end:
            if cv2.waitKey(1) == 27:
                raise KeyboardInterrupt

    print("Point the camera at the screen. Starting in 3 s...")
    show(False, 3)
    events, state = [], False
    try:
        for _ in range(flashes):
            state = not state
            events.append((time.time(), state))
            show(state, period)
    except KeyboardInterrupt:
        pass
    stop.set()
    cv2.destroyAllWindows()
    if len(events) < 4:
        sys.exit("stopped too early to measure")
    try:
        lat, contrast = analyze(frames, events)
    except ValueError as e:
        sys.exit(str(e))
    gaps = [(b[0] - a[0]) * 1000 for a, b in zip(frames, frames[1:])]
    print(f"\n{len(lat)} of {len(events)} flashes detected (contrast {contrast:.0f}/255)")
    print(f"glass-to-glass latency: median {st.median(lat):.0f} ms, min {min(lat):.0f}, max {max(lat):.0f}")
    print(f"frame interval: median {st.median(gaps):.0f} ms  (camera frame period; latency below this is not meaningful)")
    print(f"frames dropped by the reader: {feed.dropped}")


if __name__ == "__main__":
    main()
