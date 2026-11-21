"""Tests the neural depth layer: the maths, the background worker, and that switching it on does not disturb the tracker.
Skipped (passes) when onnxruntime or the model file is not available. Run: python tests/test_depth_nn.py"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "laptop"))
import cv2   # noqa: E402
import numpy as np   # noqa: E402
import depth_nn   # noqa: E402


def test():
    # --- pure maths (always runs)
    raw = np.linspace(2, 12, 100 * 100, dtype=np.float32).reshape(100, 100)
    d01, rng = depth_nn.normalize(raw)
    assert d01.min() == 0 and d01.max() == 1 and d01[0, 0] < d01[-1, -1], "near (large) must map to 1"
    d01b, rng2 = depth_nn.normalize(raw * 3, rng)            # the colour range eases toward the new scene, not all at once
    assert rng[0] < rng2[0] < 3 * rng[0] + 1 and rng2[1] < 3 * rng[1]
    frame = np.full((240, 320, 3), 128, np.uint8)
    out = depth_nn.overlay(frame, np.tile(np.linspace(0, 1, 40, dtype=np.float32), (30, 1)))
    assert out.shape == frame.shape and out.dtype == np.uint8 and not (out == frame).all()
    assert depth_nn.preprocess(frame).shape == (1, 3, 224, 308)

    # --- missing model: a clear error, never an exception
    bad = depth_nn.NeuralDepth(model_path=os.path.join(HERE, "no_such_model.onnx"))
    assert not bad.available and "cannot load the depth model" in bad.error
    bad.submit(frame)
    assert bad.latest() is None

    # --- the real network (skipped if it cannot run here)
    net = depth_nn.NeuralDepth()
    if not net.available:
        print("neural depth tests: maths ok; real-network part skipped:", net.error)
        return
    assert net.latest() is None, "nothing before the first result"
    img = np.zeros((480, 640, 3), np.uint8)
    img[:] = (90, 120, 90)
    cv2.rectangle(img, (200, 120), (440, 400), (40, 60, 220), -1)
    net.submit(img)
    t0 = time.time()
    while net.latest() is None and time.time() - t0 < 20:
        time.sleep(0.05)
    result = net.latest()
    assert result is not None, "no depth result within 20 s"
    depth, age = result
    assert depth.shape == (224, 308) and np.isfinite(depth).all() and 0 <= depth.min() and depth.max() <= 1
    net.submit(img)                                            # replaced, never queued: submit returns at once
    t = time.perf_counter()
    for _ in range(50):
        net.submit(img)
    assert time.perf_counter() - t < 0.5, "submit must not wait for the network"
    net.close()

    # --- switching the layer on through the server's frame pipeline must not disturb the teal tracker
    sys.path.insert(0, HERE)
    import app
    app.view.update(mirror=False, vflip=False, mask=False, depth=True)
    app.tracker.use_preset("teal")
    scene = np.full((480, 640, 3), 90, np.uint8)
    cv2.rectangle(scene, (300, 200), (400, 280), (128, 128, 0), -1)            # teal box (BGR)
    jpg = cv2.imencode(".jpg", scene)[1].tobytes()
    found_all = True
    for i in range(12):
        out, found = app.process(jpg, i / 15)
        found_all &= found
        time.sleep(0.15)
    assert found_all, "the tracker must still find the teal box with the depth overlay on"
    shown = cv2.imdecode(np.frombuffer(out, np.uint8), 1)
    assert shown.shape == scene.shape and not (np.abs(shown.astype(int) - scene.astype(int)).mean() < 1), "overlay should be visible"
    assert app.depth_state["net"] is not None and app.depth_state["error"] is None
    app.view["depth"] = False
    app.depth_state["net"].close()
    print("neural depth tests passed (maths, worker, tracker unaffected)")


if __name__ == "__main__":
    test()
