"""Tests the laptop side of the QNX link with no Pi: meta packets in the stream, control lines out, and how the
dashboard server uses turretd's answers. Run: python tests/test_qnx_stream.py"""
import io
import os
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "laptop"))
import cv2   # noqa: E402
import numpy as np   # noqa: E402
import receiver   # noqa: E402
import app   # noqa: E402

T_LINE = "T 41 5000 1 345.0 235.0 94.0 74.0 0.912 LOCK 1 2 1 7693 -1.0"


def packet(payload, meta=False):
    return struct.pack("<I", (receiver.META_FLAG if meta else 0) | len(payload)) + payload


class OpenStream(io.BytesIO):
    """Like a live connection: when the data runs out it waits instead of reporting the end."""

    def readinto(self, b):
        n = super().readinto(b)
        if n == 0:
            time.sleep(4)
        return n


class FakeProc:
    def __init__(self, data, open_ended=False):
        self.stdout = (OpenStream if open_ended else io.BytesIO)(data)
        self.stdin = io.BytesIO()

    def kill(self):
        pass

    def wait(self):
        pass


def test_parse():
    status, events = receiver.parse_meta("E 4990 ARMED\n" + T_LINE + "\nE 5000 FIRE #1\nT garbage\nE x y")
    assert status["found"] and status["state"] == "LOCK" and status["armed"] and status["shots"] == 2
    assert status["cx"] == 345.0 and status["confidence"] == 0.912 and status["proc_us"] == 7693 and status["link"]
    assert events == [(4990.0, "ARMED"), (5000.0, "FIRE #1")], events     # the garbled lines were skipped, not fatal
    assert receiver.parse_meta("") == (None, [])
    print("meta parsing ok")


def test_stream():
    jpg = cv2.imencode(".jpg", np.full((48, 64, 3), 100, np.uint8))[1].tobytes()
    data = packet(T_LINE.encode(), meta=True) + packet(jpg) + packet(b"E 5100 WATCHDOG: test", meta=True) + packet(jpg)
    real, seen = receiver._spawn, []
    receiver._spawn = lambda quality: FakeProc(data)
    try:
        metas, procs = [], []
        gen = receiver.jpeg_frames(on_meta=metas.append, on_proc=procs.append)
        f1, f2 = next(gen), next(gen)
        assert f1[1] == jpg and f2[1] == jpg and len(metas) == 2 and "LOCK" in metas[0] and "WATCHDOG" in metas[1]
        assert procs and procs[0] is not None
        gen.close()
        assert procs[-1] is None, "the caller is told when the Pi connection is gone"

        receiver._spawn = lambda quality: FakeProc(data, open_ended=True)
        feed = receiver.FrameFeed()                        # the same thing through the feed the server uses
        item = feed.get(-1, timeout=3)
        assert item and item[2] == jpg
        deadline = time.time() + 3
        while feed.qnx_fresh() is None and time.time() < deadline:
            time.sleep(0.02)
        m = feed.qnx_fresh()
        assert m and m["state"] == "LOCK" and feed.events and "WATCHDOG" in feed.events[-1][1]
        assert feed.qnx_fresh(max_age=0.0) is None, "an answer that is too old is not used"
    finally:
        receiver._spawn = real

    bad = packet(b"x" * 10)[:4]                            # an absurd length: reconnect, never allocate gigabytes
    receiver._spawn = lambda quality: FakeProc(struct.pack("<I", 0x7FFFFFFF) + b"junk")
    try:
        gen = receiver.jpeg_frames()
        import threading
        t = threading.Thread(target=lambda: next(gen, None), daemon=True)
        t.start()
        t.join(0.5)                                        # it must not hang reading 2 GB: it skips to the reconnect backoff
    finally:
        receiver._spawn = real
    print("stream packets, meta and heartbeat plumbing ok")


class FakeFeed:
    def __init__(self, meta):
        self.meta, self.meta_at, self.events, self.sent = meta, time.time(), [], []

    def send(self, line):
        self.sent.append(line)
        return True

    def qnx_fresh(self, max_age=0.6):
        return self.meta

    dropped = 0


def meta(**kw):
    base = {"seq": 1, "t_ms": 1000.0, "found": True, "cx": 200.0, "cy": 100.0, "w": 80.0, "h": 60.0, "confidence": 0.9,
            "state": "TRACK", "armed": False, "shots": 0, "link": True, "proc_us": 8000.0, "scan_pan": -1.0}
    base.update(kw)
    return base


def test_app():
    jpg = cv2.imencode(".jpg", np.full((480, 640, 3), 90, np.uint8))[1].tobytes()   # NOTHING teal in the picture
    ff = FakeFeed(meta())
    app.feed_ref["feed"] = ff
    app.ctl.update(tracking=False, aimer=None, error=None)
    app.view.update(mirror=False, vflip=False, mask=False, depth=False, depthcheck=False, cutout=False, box=True, qnx=True)
    app.tracker.use_preset("teal")
    app.last_seen.reset()
    app.qnx_sync["t"] = 0.0

    out, found = app.process(jpg, 100.0)
    assert found, "the target comes from QNX even though the laptop tracker would see nothing"
    assert app.tracker.confidence == 0.9
    assert app.last_seen.box[:2] == (200.0, 100.0), app.last_seen.box
    assert any(l.startswith("preset teal") for l in ff.sent) and any(l.startswith("track 0") for l in ff.sent)
    assert any(l.startswith("aim ") for l in ff.sent)
    assert app.qnx_summary()["used"] and app.qnx_summary()["state"] == "TRACK"

    app.view["mirror"] = True                              # the picture is mirrored for display: so is the answer
    app.last_seen.reset()
    app.process(jpg, 100.1)
    assert app.last_seen.box[0] == 640 - 200.0, app.last_seen.box
    app.view["vflip"] = True
    app.last_seen.reset()
    app.process(jpg, 100.2)
    assert app.last_seen.box[1] == 480 - 100.0, app.last_seen.box
    app.view.update(mirror=False, vflip=False)

    ff.meta = meta(found=False, confidence=0.0)            # QNX says: no target
    out, found = app.process(jpg, 100.3)
    assert not found and app.tracker.confidence == 0.0

    ff.meta = meta(armed=False)                            # the laptop thinks it is armed but QNX disarmed it
    app.engage.arm(True, 50.0)
    app.qnx_sync["arm_cmd"] = time.time() - 0.2            # ...just after the operator pressed ARM: give QNX a moment
    app.process(jpg, 100.4)
    assert app.engage.armed, "do not fight the operator's own ARM click"
    app.qnx_sync["arm_cmd"] = time.time() - 5.0
    app.process(jpg, 100.5)
    assert not app.engage.armed, "QNX disarmed (watchdog or interlock): the laptop follows it"

    app.view["qnx"] = False                                # layer off: the laptop tracker is used again
    out, found = app.process(jpg, 100.6)
    assert not found and not app.qnx_summary()["used"]
    app.view["qnx"] = True
    app.tracker.clear()                                    # a clicked target is not a preset: the laptop tracker handles it
    assert app.qnx_status() is None

    app.feed_ref["feed"] = None
    assert app.send_pi("arm 1") is False and app.qnx_status() is None and app.qnx_summary()["present"] is False
    app.tracker.clear()
    print("dashboard server uses QNX answers correctly (flip, disarm-follow, fallback)")


if __name__ == "__main__":
    test_parse()
    test_stream()
    test_app()
    print("qnx stream tests passed")
