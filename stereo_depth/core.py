"""Calibrated horizontal stereo, using only OpenCV core/imgproc/calib3d and NumPy."""
from dataclasses import dataclass
import math
import time
import threading

import cv2
import numpy as np


def _finite_positive(value, name):
    if not math.isfinite(value) or value <= 0:
        raise ValueError(name + " must be finite and positive")


@dataclass(frozen=True)
class Calibration:
    """OpenCV pinhole geometry; T transforms left to right, in METRES."""
    image_size: tuple
    K_left: np.ndarray
    D_left: np.ndarray
    K_right: np.ndarray
    D_right: np.ndarray
    R: np.ndarray
    T: np.ndarray
    rms_px: float
    epipolar_p95_px: float
    left_id: str
    right_id: str

    def __post_init__(self):
        size = tuple(self.image_size)
        if len(size) != 2 or any(not isinstance(v, (int, np.integer)) or v < 32 for v in size):
            raise ValueError("image_size must be (width, height), each >= 32")
        object.__setattr__(self, "image_size", size)
        for name, shape in (("K_left", (3, 3)), ("K_right", (3, 3)), ("R", (3, 3))):
            a = np.array(getattr(self, name), dtype=np.float64, copy=True)
            if a.shape != shape or not np.isfinite(a).all():
                raise ValueError("Invalid " + name)
            a.setflags(write=False)
            object.__setattr__(self, name, a)
        for name in ("D_left", "D_right", "T"):
            a = np.array(getattr(self, name), dtype=np.float64, copy=True).reshape(-1)
            lengths = (3,) if name == "T" else (4, 5, 8, 12, 14)
            if a.size not in lengths or not np.isfinite(a).all():
                raise ValueError("Invalid " + name)
            a.setflags(write=False)
            object.__setattr__(self, name, a)
        for k in (self.K_left, self.K_right):
            if k[0, 0] <= 0 or k[1, 1] <= 0 or not np.allclose(k[2], [0, 0, 1]):
                raise ValueError("Invalid camera intrinsics")
        if not np.allclose(self.R.T @ self.R, np.eye(3), atol=1e-5) or not np.isclose(np.linalg.det(self.R), 1, atol=1e-5):
            raise ValueError("R must be a rotation matrix")
        if self.T[0] >= 0 or abs(self.T[0]) <= max(abs(self.T[1]), abs(self.T[2])):
            raise ValueError("Requires horizontal stereo: right camera to the right of left (negative Tx)")
        _finite_positive(float(np.linalg.norm(self.T)), "baseline")
        for v in (self.rms_px, self.epipolar_p95_px):
            if not math.isfinite(v) or v < 0:
                raise ValueError("Calibration quality metrics must be finite and nonnegative")
        if not self.left_id or not self.right_id or self.left_id == self.right_id:
            raise ValueError("Distinct stable left_id and right_id are required")

    def save(self, path):
        with open(path, "wb") as f:
            np.savez_compressed(f, version=1, units="metres", **self.__dict__)

    @classmethod
    def load(cls, path):
        with np.load(path, allow_pickle=False) as z:
            if int(z["version"]) != 1 or str(z["units"]) != "metres":
                raise ValueError("Unsupported calibration schema/units")
            return cls(tuple(z["image_size"].tolist()), *(z[k] for k in
                ("K_left", "D_left", "K_right", "D_right", "R", "T")),
                float(z["rms_px"]), float(z["epipolar_p95_px"]), str(z["left_id"]), str(z["right_id"]))


@dataclass(frozen=True)
class Config:
    output_size: tuple = (320, 240)
    algorithm: str = "bm"
    num_disparities: int = 64
    block_size: int = 9
    min_depth_m: float = 0.15
    max_depth_m: float = 8.0
    max_skew_s: float = 0.015
    lr_tolerance_px: float = 1.0
    max_rms_px: float = 1.0
    max_epipolar_p95_px: float = 1.0

    def __post_init__(self):
        if len(self.output_size) != 2 or any(not isinstance(v, int) or v < 32 for v in self.output_size):
            raise ValueError("output_size must contain two integers >= 32")
        if self.algorithm not in ("bm", "sgbm"):
            raise ValueError("algorithm must be bm or sgbm")
        if not isinstance(self.num_disparities, int) or self.num_disparities < 16 or self.num_disparities % 16:
            raise ValueError("num_disparities must be a positive multiple of 16")
        if not isinstance(self.block_size, int) or not 5 <= self.block_size <= 51 or self.block_size % 2 == 0:
            raise ValueError("block_size must be odd, 5..51")
        if self.output_size[0] <= self.num_disparities + self.block_size or self.output_size[1] <= self.block_size:
            raise ValueError("Image too small for disparity search/block size")
        for name in ("min_depth_m", "max_depth_m", "max_skew_s", "lr_tolerance_px", "max_rms_px", "max_epipolar_p95_px"):
            _finite_positive(getattr(self, name), name)
        if self.min_depth_m >= self.max_depth_m:
            raise ValueError("min_depth_m must be below max_depth_m")


@dataclass(frozen=True)
class DepthResult:
    depth_m: np.ndarray
    disparity_px: np.ndarray
    valid: np.ndarray
    rectified_left: np.ndarray
    intrinsics: np.ndarray
    timestamps_s: object
    clock_domain: object
    skew_s: object
    processing_ms: float
    completed_monotonic_s: float
    frame_id: str
    sequence: int

    @property
    def timing_known(self):
        """Timestamp skew known; this does NOT prove exposure synchronization."""
        return self.timestamps_s is not None

    @property
    def valid_fraction(self):
        return float(np.mean(self.valid))

    def distance_at(self, x, y, radius=0):
        """Optical-axis Z in metres at RECTIFIED OUTPUT coordinates, or None.

        radius > 0 returns the median of valid neighbours, not a ray range.
        """
        if any(not isinstance(v, (int, np.integer)) for v in (x, y, radius)) or radius < 0:
            raise ValueError("Coordinates/radius must be integers; radius >= 0")
        h, w = self.depth_m.shape
        if not (0 <= x < w and 0 <= y < h):
            raise IndexError("Coordinate outside rectified output")
        patch = self.depth_m[max(0, y-radius):min(h, y+radius+1), max(0, x-radius):min(w, x+radius+1)]
        good = patch[np.isfinite(patch)]
        return float(np.median(good)) if good.size else None


def gray(image, size):
    """Accept uint8 gray or BGR only; no implicit resize, mirror, or format guessing."""
    if not isinstance(image, np.ndarray) or image.dtype != np.uint8 or image.shape[:2] != (size[1], size[0]):
        raise ValueError("Expected uint8 frame at the calibrated input resolution")
    if image.ndim == 2:
        return image
    if image.ndim == 3 and image.shape[2] == 3:
        return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    raise ValueError("Expected grayscale or BGR; convert YUY2 using adapters.yuy2_gray")


class StereoDepth:
    """One reusable processor. No camera ownership, GUI, network, or global thread settings."""
    def __init__(self, calibration, config=None):
        self.calibration = Calibration.load(calibration) if isinstance(calibration, (str, bytes)) or hasattr(calibration, "__fspath__") else calibration
        if not isinstance(self.calibration, Calibration):
            raise TypeError("calibration must be a Calibration or path")
        self.config = config or Config()
        c, cfg = self.calibration, self.config
        if c.rms_px > cfg.max_rms_px or c.epipolar_p95_px > cfg.max_epipolar_p95_px:
            raise ValueError("Calibration exceeds RMS/epipolar quality limits; recalibrate")
        if cfg.output_size[0] * c.image_size[1] != cfg.output_size[1] * c.image_size[0]:
            raise ValueError("Output aspect ratio must match calibration")
        r1, r2, p1, p2, _, roi1, roi2 = cv2.stereoRectify(
            c.K_left, c.D_left, c.K_right, c.D_right, c.image_size, c.R, c.T,
            flags=cv2.CALIB_ZERO_DISPARITY, alpha=0, newImageSize=cfg.output_size)
        self._fb = float(-p2[0, 3])
        if self._fb <= 0 or not math.isfinite(self._fb) or abs(p2[1, 3]) > 1e-6:
            raise ValueError("Rectification did not produce positive horizontal depth geometry")
        self._cx_delta = float(p1[0, 2] - p2[0, 2])
        self.intrinsics = p1[:, :3].copy()
        self.intrinsics.setflags(write=False)
        self._maps = []
        self._support = []
        for k, d, r, p in ((c.K_left, c.D_left, r1, p1), (c.K_right, c.D_right, r2, p2)):
            mx, my = cv2.initUndistortRectifyMap(k, d, r, p, cfg.output_size, cv2.CV_32FC1)
            support = ((mx >= 0) & (my >= 0) & (mx < c.image_size[0]-1) & (my < c.image_size[1]-1)).astype(np.uint8)
            self._support.append(cv2.erode(support, np.ones((cfg.block_size, cfg.block_size), np.uint8), borderType=cv2.BORDER_CONSTANT, borderValue=0).astype(bool))
            self._maps.append(cv2.convertMaps(mx, my, cv2.CV_16SC2))
        roi = cv2.getValidDisparityROI(roi1, roi2, 0, cfg.num_disparities, cfg.block_size)
        self._roi = np.zeros((cfg.output_size[1], cfg.output_size[0]), bool)
        x, y, w, h = roi
        self._roi[y:y+h, x:x+w] = True
        self._left = self._matcher(0)
        self._right_min = 1 - cfg.num_disparities
        self._right = self._matcher(self._right_min)
        self._x = np.arange(cfg.output_size[0])[None, :]
        self._y = np.arange(cfg.output_size[1])[:, None]
        self._lock = threading.Lock()
        self._sequence = 0

    def _matcher(self, minimum):
        c = self.config
        if c.algorithm == "bm":
            m = cv2.StereoBM_create(c.num_disparities, c.block_size)
            m.setMinDisparity(minimum)
            m.setTextureThreshold(10)
            m.setUniquenessRatio(12)
            m.setSpeckleWindowSize(50)
            m.setSpeckleRange(2)
            m.setDisp12MaxDiff(-1)  # Explicit independent right matcher below.
            return m
        return cv2.StereoSGBM_create(minDisparity=minimum, numDisparities=c.num_disparities,
            blockSize=c.block_size, P1=8*c.block_size**2, P2=32*c.block_size**2,
            uniquenessRatio=12, speckleWindowSize=50, speckleRange=2, disp12MaxDiff=-1,
            mode=cv2.STEREO_SGBM_MODE_SGBM)

    def process(self, left, right, *, timestamps_s=None, clock_domain=None):
        """Frames must be unmirrored, same calibration resolution, and correct left/right.

        timestamps_s=(left,right): seconds in ONE shared capture clock domain.
        Unknown timestamps are allowed for offline/static use and reported as unknown.
        Known excessive skew raises ValueError rather than publishing unreliable depth.
        Caller must not mutate inputs during this synchronous call.
        """
        skew = None
        if timestamps_s is not None:
            if len(timestamps_s) != 2 or not all(math.isfinite(t) and t >= 0 for t in timestamps_s) or not clock_domain:
                raise ValueError("Two finite nonnegative timestamps and a common clock_domain are required")
            timestamps_s = tuple(float(t) for t in timestamps_s)
            skew = abs(timestamps_s[0] - timestamps_s[1])
            if skew > self.config.max_skew_s:
                raise ValueError("Stereo frame timestamp skew exceeds configured limit")
        elif clock_domain is not None:
            raise ValueError("clock_domain requires timestamps_s")
        with self._lock:
            start = time.perf_counter()
            l = cv2.remap(gray(left, self.calibration.image_size), *self._maps[0], cv2.INTER_LINEAR)
            r = cv2.remap(gray(right, self.calibration.image_size), *self._maps[1], cv2.INTER_LINEAR)
            dl = self._left.compute(l, r).astype(np.float32) / 16.0
            dr = self._right.compute(r, l).astype(np.float32) / 16.0
            xr = np.rint(self._x - dl).astype(np.int32)
            inside = (xr >= 0) & (xr < dl.shape[1])
            ix = np.clip(xr, 0, dl.shape[1]-1)
            rd = dr[self._y, ix]
            valid = self._roi & self._support[0] & inside & self._support[1][self._y, ix]
            valid &= (dl > 0) & (rd > self._right_min - 1) & (np.abs(dl + rd) <= self.config.lr_tolerance_px)
            denom = dl - self._cx_delta
            depth = np.full(dl.shape, np.nan, np.float32)
            np.divide(self._fb, denom, out=depth, where=valid & (denom > 0))
            valid &= np.isfinite(depth) & (depth >= self.config.min_depth_m) & (depth <= self.config.max_depth_m)
            depth[~valid] = np.nan
            dl[~valid] = np.nan
            for a in (depth, dl, valid, l):
                a.setflags(write=False)
            self._sequence += 1
            return DepthResult(depth, dl, valid, l, self.intrinsics, timestamps_s, clock_domain, skew,
                (time.perf_counter()-start)*1000, time.monotonic(), self.calibration.left_id + "/rectified", self._sequence)
