"""Tests that FrameFeed always hands over the newest frame. Run: python tests/test_feed.py"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "laptop"))
import receiver   # noqa: E402


def test_slow_start():
    """Asking for a frame before the first one has arrived must wait for it, not return garbage or crash."""
    def slow_stream(quality):
        time.sleep(0.4)                       # the Pi takes a while to start streaming
        yield time.time(), b"first"
        time.sleep(10)
    receiver.jpeg_frames = slow_stream
    feed = receiver.FrameFeed()
    t0 = time.time()
    assert feed.get(-1, timeout=0.1) is None, "no frame yet: must time out cleanly"
    item = feed.get(-1, timeout=3)
    assert item is not None and item[2] == b"first", item
    assert time.time() - t0 >= 0.35, "should have waited for the stream to start"
    assert feed.get(item[0], timeout=0.1) is None, "no newer frame than the one just taken"
    print("slow-start feed test passed")


def test():
    test_slow_start()
    def fake_stream(quality):                 # 100 frames at ~100 fps, numbered 0..99
        for i in range(100):
            yield time.time(), str(i).encode()
            time.sleep(0.01)

    receiver.jpeg_frames = fake_stream
    feed = receiver.FrameFeed()
    seq, seen = -1, []
    t0 = time.time()
    while time.time() - t0 < 3:
        item = feed.get(seq, timeout=0.3)
        if item is None:
            if seen and seen[-1] == b"99":
                break
            continue
        seq, ts, jpg = item
        seen.append(jpg)
        time.sleep(0.05)                      # a slow consumer: ~20 frames/s against 100 frames/s
    nums = [int(x) for x in seen]
    assert nums == sorted(nums), "frames must arrive in order"
    assert nums[-1] == 99, f"the newest frame must be delivered, last was {nums[-1]}"
    assert feed.dropped > 50, f"stale frames should be dropped, only {feed.dropped} were"
    assert len(nums) + feed.dropped >= 95, (len(nums), feed.dropped)
    print(f"frame feed tests passed (consumed {len(nums)}, dropped {feed.dropped})")


if __name__ == "__main__":
    test()
