"""Tests that FrameFeed always hands over the newest frame. Run: python tests/test_feed.py"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "laptop"))
import receiver   # noqa: E402


def test_slow_start():
    """Asking for a frame before the first one has arrived must wait for it, not return garbage or crash."""
    def slow_stream(quality, *args):
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
    def fake_stream(quality, *args):               # 100 frames at ~100 fps, numbered 0..99
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


def test_reader_survives_errors():
    """A crash inside the stream reader must not kill the video for good: it is logged and the reader reconnects."""
    calls = []

    def flaky_stream(quality, *args):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("simulated crash in the stream")
        yield time.time(), b"after the crash"
        time.sleep(10)
    real_sleep, real_jpeg = time.sleep, receiver.jpeg_frames
    receiver.jpeg_frames = flaky_stream
    receiver.ERROR_LOG = os.path.join(os.environ.get("TEMP", "."), "feed_errors_test.log")
    if os.path.exists(receiver.ERROR_LOG):
        os.remove(receiver.ERROR_LOG)
    try:
        feed = receiver.FrameFeed()
        item = feed.get(-1, timeout=5)
        assert item is not None and item[2] == b"after the crash", "the reader must reconnect after an error"
        assert len(calls) >= 2 and "simulated crash" in open(receiver.ERROR_LOG, encoding="utf-8").read()
    finally:
        receiver.jpeg_frames = real_jpeg
    print("reader survives errors ok")


def test_wire_stats():
    """The laptop measures what really arrived over the Wi-Fi: rate, size and the gaps between pictures; and parses the Pi's own
    'V' report."""
    feed = receiver.FrameFeed.__new__(receiver.FrameFeed)
    from collections import deque
    feed._arrivals = deque(maxlen=600)
    assert feed.wire_stats() is None, "not enough data yet"
    now = time.time()
    for i in range(40):                                   # 40 pictures of 30 KB, 66 ms apart, with one 400 ms stall
        t = now - 3.0 + i * 0.066 + (0.334 if i >= 20 else 0.0)
        feed._arrivals.append((t, 30 * 1024, 80.0 + (i % 5)))     # each picture took 80-84 ms to arrive
    s = feed.wire_stats()
    assert s["kb"] == 30.0 and s["n"] == 40 and s["over_200"] == 1 and 395 < s["gap_max"] < 405, s
    assert 60 < s["gap_p50"] < 70 and s["gap_p95"] < 400, s
    assert 80 <= s["delay_p50"] <= 84 and s["delay_p95"] <= 84, s
    assert receiver.parse_pongs("P u1 123.5\nP s2") == [("u1", 123.5), ("s2", None)] and receiver.parse_stamp("E 1 x\nF 55.25") == 55.25
    assert receiver.parse_video_stats("V 14.8 21.3 3.2 18") == {"fps": 14.8, "kb": 21.3, "encode_ms": 3.2, "cpu_pct": 18.0}
    assert receiver.parse_video_stats("V 1 2 3") is None and receiver.parse_video_stats("C ok 0.1") is None
    print("wire stats ok")


if __name__ == "__main__":
    test()
    test_reader_survives_errors()
    test_wire_stats()
