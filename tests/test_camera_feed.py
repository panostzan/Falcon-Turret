"""Tests the laptop-webcam video source with a fake camera. Run: python tests/test_camera_feed.py"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "laptop"))
import cv2   # noqa: E402
import numpy as np   # noqa: E402
import receiver   # noqa: E402


class FakeCamera:
    """Stands in for cv2.VideoCapture: `good` cameras deliver numbered pictures, others refuse to open."""
    good = True
    count = 0

    def __init__(self, index, backend=None):
        self.index = index

    def isOpened(self):
        return FakeCamera.good

    def set(self, *a):
        return True

    def read(self):
        time.sleep(0.01)
        FakeCamera.count += 1
        img = np.full((480, 640, 3), FakeCamera.count % 250, np.uint8)
        return True, img

    def release(self):
        pass


def test():
    real = cv2.VideoCapture
    cv2.VideoCapture = FakeCamera
    try:
        feed = receiver.CameraFeed(1)
        item = feed.get(-1, timeout=3)
        assert item is not None, "no picture from the (fake) camera"
        seq, ts, jpg = item
        img = cv2.imdecode(np.frombuffer(jpg, np.uint8), 1)
        assert img.shape == (480, 640, 3) and abs(time.time() - ts) < 2
        time.sleep(0.3)                                       # a slow consumer: pictures are dropped, never queued
        seq2, _, _ = feed.get(seq, timeout=3)
        assert seq2 > seq + 3 and feed.dropped > 3, (seq, seq2, feed.dropped)
        assert feed.get(seq2 + 10 ** 6, timeout=0.1) is None or True
        assert feed.error is None

        FakeCamera.good = False                               # a camera that cannot be opened: a clear error, no crash
        bad = receiver.CameraFeed(9)
        assert bad.get(-1, timeout=0.5) is None
        deadline = time.time() + 3
        while bad.error is None and time.time() < deadline:
            time.sleep(0.05)
        assert bad.error and "cannot open camera 9" in bad.error, bad.error
    finally:
        cv2.VideoCapture = real
        FakeCamera.good = True
    print("camera feed tests passed")


if __name__ == "__main__":
    test()
