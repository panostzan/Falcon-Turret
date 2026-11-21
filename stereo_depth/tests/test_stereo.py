"""Independent numerical, lifecycle, calibration and input-contract tests."""
from dataclasses import replace
import tempfile
from pathlib import Path
import threading
import time
import unittest

import cv2
import numpy as np

from stereo_depth import Calibration, Config, StereoDepth, StereoWorker, Frame, FramePairer
from stereo_depth.__main__ import synthetic_pair
from stereo_depth.adapters import yuy2_gray
from stereo_depth.calibrate import fit_points


class DepthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.l, cls.r, cls.cal = synthetic_pair()

    def processor(self, **kwargs):
        return StereoDepth(self.cal, Config(**kwargs))

    def test_metric_depth_both_matchers(self):
        for algorithm in ("bm", "sgbm"):
            with self.subTest(algorithm=algorithm):
                result = self.processor(algorithm=algorithm).process(self.l, self.r)
                self.assertGreater(result.valid_fraction, .4)
                self.assertAlmostEqual(float(np.nanmedian(result.disparity_px)), 12, delta=.15)
                self.assertAlmostEqual(float(np.nanmedian(result.depth_m)), 2, delta=.03)
                self.assertAlmostEqual(result.distance_at(160, 120, 2), 2, delta=.03)
                self.assertIsNone(result.distance_at(0, 0))
                self.assertFalse(result.timing_known)
                self.assertFalse(result.depth_m.flags.writeable)
                self.assertTrue(np.isnan(result.depth_m[~result.valid]).all())

    def test_resolution_scaling(self):
        # 640px calibration, compute at 320: focal length and disparity both halve.
        l, r, c = synthetic_pair(640, 480, 24)
        k = c.K_left.copy()
        k[0, 0] = k[1, 1] = 480
        c = replace(c, K_left=k, K_right=k)
        result = StereoDepth(c).process(l, r)
        self.assertAlmostEqual(float(np.nanmedian(result.depth_m)), 2, delta=.05)
        self.assertAlmostEqual(result.intrinsics[0, 0], 240, delta=1)

    def test_flat_images_invalid(self):
        image = np.full(self.l.shape, 128, np.uint8)
        result = self.processor().process(image, image)
        self.assertEqual(result.valid_fraction, 0)
        self.assertIsNone(result.distance_at(160, 120, 3))

    def test_swapped_images_not_false_two_metre_plane(self):
        result = self.processor().process(self.r, self.l)
        self.assertLess(result.valid_fraction, .05)

    def test_occlusion_invalid(self):
        right = self.r.copy()
        right[:, 145:200] = 0
        result = self.processor().process(self.l, right)
        self.assertLess(np.mean(result.valid[20:-20, 170:200]), .05)

    def test_range_clipping(self):
        result = self.processor(max_depth_m=1).process(self.l, self.r)
        self.assertEqual(result.valid_fraction, 0)

    def test_timing(self):
        p = self.processor()
        result = p.process(self.l, self.r, timestamps_s=(1., 1.005), clock_domain="qnx-monotonic")
        self.assertAlmostEqual(result.skew_s, .005)
        self.assertTrue(result.timing_known)
        for ts, domain in (((1., 1.1), "qnx"), ((float('nan'), 1), "qnx"), ((1, 1), None), (None, "qnx")):
            with self.assertRaises(ValueError):
                p.process(self.l, self.r, timestamps_s=ts, clock_domain=domain)

    def test_bad_images(self):
        p = self.processor()
        for bad in (None, self.l.astype(float), self.l[:-1], np.zeros((240, 320, 4), np.uint8)):
            with self.assertRaises(ValueError):
                p.process(bad, self.r)

    def test_result_lifetime(self):
        p = self.processor()
        a = p.process(self.l, self.r)
        original = a.depth_m.copy()
        p.process(np.zeros_like(self.l), np.zeros_like(self.r))
        np.testing.assert_equal(a.depth_m, original)
        with self.assertRaises(IndexError):
            a.distance_at(320, 10)

    def test_calibration_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"cal.npz"
            self.cal.save(path)
            loaded = Calibration.load(path)
            np.testing.assert_equal(loaded.T, self.cal.T)
            self.assertEqual(loaded.left_id, self.cal.left_id)
            StereoDepth(path)

    def test_invalid_geometry_quality_config(self):
        for changes in ({"T": [0, 0, 0]}, {"T": [.1, 0, 0]}, {"R": np.ones((3, 3))},
                        {"rms_px": float('nan')}, {"right_id": self.cal.left_id}):
            with self.assertRaises(ValueError):
                replace(self.cal, **changes)
        with self.assertRaises(ValueError):
            StereoDepth(replace(self.cal, rms_px=4))
        for changes in ({"num_disparities": 15}, {"block_size": 8}, {"max_skew_s": -1},
                        {"output_size": (64, 48)}, {"max_depth_m": .1}):
            with self.assertRaises(ValueError):
                Config(**changes)
        with self.assertRaises(ValueError):
            self.processor(output_size=(320, 200))

    def test_yuy2_stride(self):
        raw = bytearray([10, 128, 20, 128, 9, 9, 30, 128, 40, 128, 9, 9])
        image = yuy2_gray(raw, 2, 2, 6)
        np.testing.assert_equal(image, [[10, 20], [30, 40]])
        raw[0] = 42
        self.assertEqual(image[0, 0], 42)
        with self.assertRaises(ValueError):
            yuy2_gray(raw[:4], 2, 2, 6)


class StreamTests(unittest.TestCase):
    def frame(self, side, ts, domain="capture"):
        return Frame(np.zeros((2, 2), np.uint8), ts, side, domain)

    def test_pairing_skew_drops_no_reuse_and_copy(self):
        p = FramePairer("L", "R")
        original = self.frame("L", 1.)
        self.assertIsNone(p.push("left", original))
        original.image[:] = 255
        pair = p.push("right", self.frame("R", 1.005))
        self.assertEqual(pair[0].image[0, 0], 0)
        self.assertIsNone(p.push("right", self.frame("R", 2.)))
        self.assertIsNone(p.push("left", self.frame("L", 3.)))
        self.assertEqual(p.dropped, 1)
        self.assertIsNotNone(p.push("right", self.frame("R", 3.001)))

    def test_pairer_contract(self):
        p = FramePairer("L", "R", capacity=1)
        p.push("left", self.frame("L", 1.))
        p.push("left", self.frame("L", 2.))
        self.assertEqual(p.dropped, 1)
        for side, frame in (("left", self.frame("L", 2.)), ("left", self.frame("R", 3.)),
                            ("right", self.frame("R", 3., "other"))):
            with self.assertRaises(ValueError):
                p.push(side, frame)

    def test_worker_shutdown_staleness(self):
        l, r, c = synthetic_pair()
        worker = StereoWorker(StereoDepth(c), max_age_s=.5)
        try:
            worker.submit(l, r, timestamps_s=(1, 1), clock_domain="capture")
            deadline = time.monotonic()+2
            while worker.latest() is None and time.monotonic() < deadline:
                time.sleep(.005)
            self.assertIsNotNone(worker.latest(), worker.error)
            time.sleep(.51)
            self.assertIsNone(worker.latest())
        finally:
            worker.close()
        self.assertFalse(worker._thread.is_alive())
        with self.assertRaises(RuntimeError):
            worker.submit(l, r, timestamps_s=(2, 2), clock_domain="capture")

    def test_worker_queue_bounded_and_copies(self):
        l, r, c = synthetic_pair()
        p = StereoDepth(c)
        entered, release = threading.Event(), threading.Event()
        real = p.process
        observations = []
        def slow(left, right, **kwargs):
            observations.append(int(left[0, 0]))
            entered.set()
            release.wait(2)
            return real(left, right, **kwargs)
        p.process = slow
        w = StereoWorker(p, max_age_s=5)
        try:
            w.submit(l, r, timestamps_s=(1, 1), clock_domain="capture")
            self.assertTrue(entered.wait(1))
            image = l.copy()
            image[0, 0] = 17
            w.submit(image, r, timestamps_s=(2, 2), clock_domain="capture")
            w.submit(image, r, timestamps_s=(3, 3), clock_domain="capture")
            image[:] = 0
            self.assertEqual(w.dropped, 1)
            release.set()
            deadline = time.monotonic()+2
            while len(observations) < 2 and time.monotonic() < deadline:
                time.sleep(.005)
            self.assertEqual(observations[-1], 17)
        finally:
            release.set()
            w.close()


class CalibrationTests(unittest.TestCase):
    def test_recovers_geometry_from_projected_checkerboard(self):
        rng = np.random.default_rng(42)
        k = np.array([[500., 0, 320], [0, 500., 240], [0, 0, 1.]])
        obj = np.zeros((54, 3), np.float32)
        obj[:, :2] = np.mgrid[0:9, 0:6].T.reshape(-1, 2)*.025
        objects, lp, rp = [], [], []
        for i in range(25):
            rv = rng.uniform(-.35, .35, 3)
            tv = np.array([rng.uniform(-.2, .1), rng.uniform(-.16, .1), rng.uniform(.7, 1.1)])
            l, _ = cv2.projectPoints(obj, rv, tv, k, np.zeros(5))
            r, _ = cv2.projectPoints(obj, rv, tv+np.array([-.1, 0, 0]), k, np.zeros(5))
            objects.append(obj)
            lp.append(l)
            rp.append(r)
        cal, report = fit_points(objects, lp, rp, (640, 480), "L", "R", expected_baseline_m=.1)
        self.assertAlmostEqual(float(np.linalg.norm(cal.T)), .1, delta=.002)
        self.assertLess(report["held_out_epipolar_p95_px"], .1)
        self.assertEqual(report["held_out_views"], 5)
        with self.assertRaises(ValueError):
            fit_points(objects[:5], lp[:5], rp[:5], (640, 480), "L", "R")
        wrong = [p + rng.normal(0, 5, p.shape).astype(np.float32) for p in rp]
        with self.assertRaises(ValueError):
            fit_points(objects, lp, wrong, (640, 480), "L", "R")


if __name__ == "__main__":
    unittest.main()
