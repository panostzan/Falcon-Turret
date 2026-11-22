import unittest

import cv2
import numpy as np

from stereo_depth.experimental import ExperimentalStereo


class ExperimentalTests(unittest.TestCase):
    def test_no_texture_rejected(self):
        e = ExperimentalStereo()
        blank = np.zeros((360, 480, 3), np.uint8)
        with self.assertRaises(ValueError):
            e.align(blank, blank)
        self.assertFalse(e.ready)

    def test_nonplanar_correspondences_and_disparity(self):
        e = ExperimentalStereo()
        rng = np.random.default_rng(8)
        a = rng.uniform([40, 30], [440, 330], (500, 2)).astype(np.float32)
        b = a.copy()
        b[:, 0] -= np.where(a[:, 0] > 240, 24., 12.)
        e.align_points(a, b)
        self.assertTrue(e.ready)
        left = rng.integers(0, 256, (360, 480), np.uint8)
        right = rng.integers(0, 256, (360, 480), np.uint8)
        right[:, :228] = left[:, 12:240]
        right[:, 216:456] = left[:, 240:480]
        result = e.process(left, right)
        self.assertGreater(float(result.valid.mean()), .25)
        self.assertTrue(np.isnan(result.disparity_px[~result.valid]).all())
        self.assertFalse(hasattr(result, "depth_m"))
        flat = e.process(np.zeros_like(left), np.zeros_like(right))
        self.assertEqual(float(flat.valid.mean()), 0.)
        e.reset()
        with self.assertRaises(RuntimeError):
            e.process(left, right)


if __name__ == "__main__":
    unittest.main()
