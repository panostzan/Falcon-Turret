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
        self.udp_answers = 0

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


def test_udp_channel():
    """The UDP channel with a pretend Pi: control lines go out on it, answers and timing probes come back on it, and an older
    copy of an answer (arriving later through the video stream) never replaces a newer one."""
    import socket
    import threading
    pi = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    pi.bind(("127.0.0.1", 0))
    pi.settimeout(0.5)
    got = []

    def serve():
        while True:
            try:
                data, addr = pi.recvfrom(512)
            except socket.timeout:
                continue
            except OSError:
                return
            for line in data.decode().splitlines():
                got.append(line)
                if line.startswith("ping "):
                    pi.sendto(("P " + line[5:]).encode(), addr)
                elif line == "hb":
                    pi.sendto(T_LINE.replace("5000", "7000").encode(), addr)     # status stamped t_ms 7000
    threading.Thread(target=serve, daemon=True).start()
    real_spawn, real_port = receiver._spawn, receiver.UDP_PORT
    receiver.UDP_PORT = pi.getsockname()[1]
    receiver.current_ip[0] = "127.0.0.1"
    receiver._spawn = lambda quality: FakeProc(b"", open_ended=True)
    try:
        feed = receiver.FrameFeed()
        deadline = time.time() + 5
        while time.time() < deadline and not (feed.rtt()["udp"]["n"] and feed.meta):
            time.sleep(0.05)
        assert "hb" in got, "heartbeats go out over UDP"
        assert feed.udp_answers > 0 and feed.meta and feed.meta["t_ms"] == 7000.0, feed.meta
        assert feed.rtt()["udp"]["n"] >= 1 and feed.rtt()["udp"]["p50"] < 100, feed.rtt()
        assert any(l.startswith("ping u") for l in got) and not any(l.startswith("ping s") for l in got), "UDP probes only go by UDP"
        assert feed.send("arm 1") and "arm 1" in got or time.sleep(0.3) or "arm 1" in got
        feed._on_meta(T_LINE)                                                  # the same answer, older (t_ms 5000), via the stream
        assert feed.meta["t_ms"] == 7000.0, "an older answer must not replace a newer one"
        feed._pings["s99"] = time.time() - 0.04
        feed._on_meta("P s99")                                                 # a probe answered through the stream
        assert feed.rtt()["stream"]["n"] == 1 and 30 < feed.rtt()["stream"]["p50"] < 200
    finally:
        receiver._spawn, receiver.UDP_PORT = real_spawn, real_port
        receiver.current_ip[0] = None
        pi.close()
    print("udp channel ok")


def test_aim_loop():
    """Aiming runs on the Pi's answers (not on video frames): the step maps the position (with the flips) and stops the picture
    loop from aiming a second time."""
    class FakeAimer:
        def __init__(self):
            self.calls, self.settled, self.resets = [], True, 0

        def update(self, ex, ey):
            self.calls.append((round(ex, 3), round(ey, 3)))
            return (90, 22)

        def reset(self):
            self.resets += 1

    app.view.update(mirror=False, vflip=False)
    a = FakeAimer()
    assert app.qnx_aim_step(meta(cx=480.0, cy=120.0), a) == (90, 22)
    assert a.calls[-1] == (0.5, -0.5), a.calls                 # 480 of 640 is half way to the right; 120 of 480 is half way up
    app.view["mirror"] = True
    app.qnx_aim_step(meta(cx=480.0, cy=120.0), a)
    assert a.calls[-1] == (-0.5, -0.5), "mirrored: the same spot is on the other side of the picture"
    app.view.update(mirror=False, vflip=True)
    app.qnx_aim_step(meta(cx=320.0, cy=120.0), a)
    assert a.calls[-1] == (0.0, 0.5)
    app.view["vflip"] = False
    n = len(a.calls)
    assert app.qnx_aim_step(meta(found=False, cx=0.0, cy=0.0), a) is None and a.resets == 1 and len(a.calls) == n

    ff = FakeFeed(meta())
    app.feed_ref["feed"] = ff
    app.tracker.use_preset("teal")
    app.ctl.update(tracking=True, aimer=a, error=None)
    assert app.qnx_aim_active(), "tracking on + QNX answering: the aim loop owns aiming"
    app.ctl["tracking"] = False
    assert not app.qnx_aim_active()
    app.ctl.update(tracking=False, aimer=None)
    app.feed_ref["feed"] = None
    app.tracker.clear()

    # the feed's wait: returns the moment a NEWER answer arrives, and not for an old one
    feed = receiver.FrameFeed.__new__(receiver.FrameFeed)
    feed._meta_cv = __import__("threading").Condition()
    feed.meta = {"t_ms": 100.0}
    assert receiver.FrameFeed.wait_meta(feed, 100.0, timeout=0.05) is None
    assert receiver.FrameFeed.wait_meta(feed, 50.0, timeout=0.05)["t_ms"] == 100.0
    print("aim loop pieces ok")


def test_camera_status():
    """The Pi's camera report ('C ...') is parsed, kept fresh for 3 s, and summarised for the dashboard."""
    assert receiver.parse_camera("C ok 0.1") == {"state": "ok", "age": 0.1, "usb": None}
    assert receiver.parse_camera("E 1 x\nC stalled 7.5 nousb") == {"state": "stalled", "age": 7.5, "usb": False}
    assert receiver.parse_camera("C stalled 3.0 usb")["usb"] is True
    assert receiver.parse_camera("C stalled 3.0 unknown")["usb"] is None
    assert receiver.parse_camera("C bogus 1") is None and receiver.parse_camera("T 1 2 3") is None and receiver.parse_camera("C ok x") is None
    feed = receiver.FrameFeed.__new__(receiver.FrameFeed)
    feed.meta, feed.events, feed._pings, feed.rtt_ms, feed.camera, feed.camera_at = None, [], {}, {"udp": [], "stream": []}, None, 0.0
    feed._meta_cv = __import__("threading").Condition()
    feed._on_meta("C stalled 4.0 nousb", via="udp")
    app.feed_ref["feed"] = feed
    cam = app.camera_summary()
    assert cam["state"] == "stalled" and cam["usb"] is False and cam["source"] == "pi", cam
    feed.camera_at = time.time() - 10
    assert app.camera_summary()["state"] == "unknown", "an old report is not shown as current"
    app.feed_ref["feed"] = None
    assert app.camera_summary()["state"] == "unknown"
    print("camera status ok")


def test_app():
    jpg = cv2.imencode(".jpg", np.full((480, 640, 3), 90, np.uint8))[1].tobytes()   # NOTHING teal in the picture
    ff = FakeFeed(meta())
    app.feed_ref["feed"] = ff
    app.ctl.update(tracking=False, aimer=None, error=None)
    app.view.update(mirror=False, vflip=False, mask=False, depth=False, depthcheck=False, box=True, qnx=True)
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

    teal = np.full((480, 640, 3), 90, np.uint8)
    cv2.rectangle(teal, (300, 200), (380, 280), (170, 170, 20), -1)
    teal_jpg = cv2.imencode(".jpg", teal)[1].tobytes()
    app.view["mask"] = True                                # Color match layer on while QNX tracks: the mask must still be produced
    app.tracker.debug_mask = None
    out, found = app.process(teal_jpg, 100.01)
    assert found and app.tracker.debug_mask is not None and app.tracker.debug_mask.any(), "Color match works under QNX tracking"
    assert app.tracker.confidence == 0.9, "and the confidence is still QNX's"
    app.view["mask"] = False

    small = cv2.imencode(".jpg", np.full((360, 480, 3), 90, np.uint8))[1].tobytes()     # the dashboard picture can be 480x360
    ff.meta = meta(cx=320.0, cy=240.0, w=80.0, h=60.0)                                   # the Pi reports in its 640x480 picture
    app.view["mirror"] = False
    app.last_seen.reset()
    app.process(small, 100.05)
    assert app.last_seen.box == (240.0, 180.0, 60.0, 45.0), app.last_seen.box            # scaled by 3/4 onto the smaller picture
    ff.meta = meta()
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
    test_udp_channel()
    test_aim_loop()
    test_camera_status()
    test_app()
    print("qnx stream tests passed")
