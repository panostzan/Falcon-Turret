"""Checks the C tracker (qnx/tracker.c) against the Python tracker on the same pictures. Builds the C code with whatever
C compiler is on this machine (gcc, clang, zig cc, MSVC cl); passes with a note if there is none.
Run: python tests/test_qnx_tracker.py"""
import ctypes
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "laptop"))
import cv2   # noqa: E402
import numpy as np   # noqa: E402
from tracker import ColorTracker   # noqa: E402

SRC = os.path.join(HERE, "..", "qnx", "tracker.c")


class Result(ctypes.Structure):
    _fields_ = [("found", ctypes.c_int)] + [(n, ctypes.c_double) for n in
                ("cx", "cy", "w", "h", "confidence", "fill", "size", "unique", "steady", "seen")]


def build():
    out = os.path.join(tempfile.mkdtemp(), "turret_tracker.dll" if os.name == "nt" else "libturret_tracker.so")
    candidates = [["gcc"], ["clang"], ["cc"], [sys.executable, "-m", "ziglang", "cc"]]
    for cc in candidates:
        if cc[0] not in (sys.executable,) and not shutil.which(cc[0]):
            continue
        cmd = cc + ["-O2", "-std=c99", "-Wall", "-Wextra", "-shared", "-o", out, SRC] + ([] if os.name == "nt" else ["-fPIC", "-lm"])
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 0:
            if r.stderr.strip():
                print("compiler warnings:\n" + r.stderr)
            return out
        if cc[0] != sys.executable:
            print("compile failed with", cc[0], "\n", r.stderr)
            return None
    return None


def load(path):
    lib = ctypes.CDLL(path)
    lib.tracker_new.restype = ctypes.c_void_p
    lib.tracker_new.argtypes = [ctypes.c_int, ctypes.c_int]
    lib.tracker_free.argtypes = [ctypes.c_void_p]
    lib.tracker_use_preset.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    lib.tracker_clear.argtypes = [ctypes.c_void_p]
    lib.tracker_find_bgr.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(Result)]
    lib.tracker_find_yuy2.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(Result)]
    return lib


def scene(boxes, rival=None):
    img = np.full((480, 640, 3), 90, np.uint8)
    cv2.rectangle(img, (0, 0), (200, 300), (225, 225, 225), -1)               # a white wall
    for (x, y, w, h) in boxes:
        cv2.rectangle(img, (x, y), (x + w, y + h), (128, 128, 0), -1)         # teal (BGR)
        cv2.rectangle(img, (x, y + h // 2 - 4), (x + w, y + h // 2 + 4), (250, 250, 250), -1)   # glare strip
    return img


def bgr_to_yuy2(bgr):
    ycc = cv2.cvtColor(bgr, cv2.COLOR_BGR2YCrCb).astype(np.float32)           # full-range BT.601, like the JPEG path
    y, cr, cb = ycc[..., 0], ycc[..., 1], ycc[..., 2]
    out = np.empty((bgr.shape[0], bgr.shape[1] * 2), np.uint8)
    out[:, 0::4] = y[:, 0::2]
    out[:, 2::4] = y[:, 1::2]
    out[:, 1::4] = ((cb[:, 0::2] + cb[:, 1::2]) / 2).round()
    out[:, 3::4] = ((cr[:, 0::2] + cr[:, 1::2]) / 2).round()
    return out


def test():
    path = build()
    if path is None:
        print("no C compiler found: C tracker not built here (install gcc/clang, or `pip install ziglang`); skipped")
        return
    lib = load(path)
    t = lib.tracker_new(640, 480)
    assert t and lib.tracker_use_preset(t, b"nonsense") == -1 and lib.tracker_use_preset(t, b"teal") == 0
    py = ColorTracker()
    py.use_preset("teal")
    res = Result()

    positions = [(300, 200), (312, 204), (326, 210), (340, 214), (352, 222), (366, 226), (380, 232), (392, 236),
                 (404, 240), (416, 246), (430, 250), (440, 254)]
    worst = 0.0
    for (x, y) in positions:
        img = scene([(x, y, 90, 70)])
        p = py.find(img)
        assert p is not None
        c_found = lib.tracker_find_bgr(t, img.ctypes.data, ctypes.byref(res))
        assert c_found == 1 and res.found == 1, "C tracker missed a target the Python one found"
        err = max(abs(res.cx - p[0]), abs(res.cy - p[1]))
        worst = max(worst, err)
        assert err < 6.0, f"centre differs by {err:.1f}px at {x},{y}: C ({res.cx:.1f},{res.cy:.1f}) py ({p[0]:.1f},{p[1]:.1f})"
        assert abs(res.w - p[2]) < 10 and abs(res.h - p[3]) < 10, (res.w, res.h, p)
    assert abs(res.confidence - py.confidence) < 0.12, (res.confidence, py.confidence)
    assert res.confidence > 0.85

    # YUY2 input (the camera's own format) must agree with the BGR path
    lib.tracker_clear(t); lib.tracker_use_preset(t, b"teal")
    img = scene([(300, 200, 90, 70)])
    yuy = bgr_to_yuy2(img)
    for _ in range(3):
        assert lib.tracker_find_yuy2(t, yuy.ctypes.data, yuy.shape[1], ctypes.byref(res)) == 1
    assert abs(res.cx - 345) < 8 and abs(res.cy - 235) < 8, (res.cx, res.cy)

    # nothing there: not found, confidence 0; a rival blob of similar size lowers "unique"
    empty = scene([])
    assert lib.tracker_find_bgr(t, empty.ctypes.data, ctypes.byref(res)) == 0 and res.confidence == 0.0
    lib.tracker_clear(t); lib.tracker_use_preset(t, b"teal")
    two = scene([(300, 200, 90, 70), (420, 330, 86, 66)])
    for _ in range(3):
        lib.tracker_find_bgr(t, two.ctypes.data, ctypes.byref(res))
    assert res.unique < 0.4, res.unique

    # unarmed: nothing found
    lib.tracker_clear(t)
    assert lib.tracker_find_bgr(t, img.ctypes.data, ctypes.byref(res)) == 0

    # speed (informational)
    import time
    lib.tracker_use_preset(t, b"teal")
    t0 = time.perf_counter()
    for _ in range(50):
        lib.tracker_find_bgr(t, img.ctypes.data, ctypes.byref(res))
    ms = (time.perf_counter() - t0) / 50 * 1000
    t1 = time.perf_counter()
    for _ in range(50):
        py.find(img)
    pms = (time.perf_counter() - t1) / 50 * 1000
    lib.tracker_free(t)
    print(f"C tracker matches Python (worst centre difference {worst:.1f}px); C {ms:.1f} ms/frame vs Python+OpenCV {pms:.1f} ms")


if __name__ == "__main__":
    test()
