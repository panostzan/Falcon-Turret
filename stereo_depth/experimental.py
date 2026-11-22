"""Natural-feature rectification + signed disparity; deliberately NOT metric depth.

Uncalibrated projective rectification does not establish globally comparable
physical depth or a metre scale. This is a diagnostic precursor to calibration.
"""
import argparse
from dataclasses import dataclass
from pathlib import Path
import time

import cv2
import numpy as np


@dataclass(frozen=True)
class DisparityResult:
    left: np.ndarray
    right: np.ndarray
    disparity_px: np.ndarray
    valid: np.ndarray
    processing_ms: float


class ExperimentalStereo:
    def __init__(self, size=(480, 360)):
        self.size = size
        self.ready = False
        self.status = "Looking for shared scene features; hold cameras still"
        self._sift = cv2.SIFT_create(nfeatures=2200, contrastThreshold=.025)
        self._bf = cv2.BFMatcher(cv2.NORM_L2)

    def reset(self):
        self.ready = False
        self.status = "Alignment reset; hold the cameras still"

    def _gray(self, image):
        if image is None or image.dtype != np.uint8:
            raise ValueError("Expected uint8 camera image")
        image = cv2.resize(image, self.size, interpolation=cv2.INTER_AREA)
        return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image

    def align(self, left, right):
        self.ready = False
        l, r = self._gray(left), self._gray(right)
        kl, dl = self._sift.detectAndCompute(l, None)
        kr, dr = self._sift.detectAndCompute(r, None)
        if dl is None or dr is None:
            raise ValueError("Not enough texture: point both cameras at the same detailed scene")
        forward = self._bf.knnMatch(dl, dr, k=2)
        backward = self._bf.knnMatch(dr, dl, k=2)
        back = {m.queryIdx: m.trainIdx for pair in backward if len(pair) == 2
                for m, n in [pair] if m.distance < .75*n.distance}
        matches = [m for pair in forward if len(pair) == 2 for m, n in [pair]
                   if m.distance < .75*n.distance and back.get(m.trainIdx) == m.queryIdx]
        if len(matches) < 50:
            raise ValueError(f"Only {len(matches)} shared features; need 50. Increase overlapping textured view")
        a = np.float32([kl[m.queryIdx].pt for m in matches])
        b = np.float32([kr[m.trainIdx].pt for m in matches])
        return self.align_points(a, b)

    def align_points(self, a, b):
        """Fit using 75% of matches, validate on untouched 25%."""
        self.ready = False
        a, b = np.asarray(a, np.float32), np.asarray(b, np.float32)
        if a.shape != b.shape or a.ndim != 2 or a.shape[1] != 2 or len(a) < 50 or not np.isfinite([a, b]).all():
            raise ValueError("Need >=50 finite matching points")
        hold = np.arange(len(a)) % 4 == 0
        f, mask = cv2.findFundamentalMat(a[~hold], b[~hold], cv2.FM_RANSAC, 1.0, .999, 10000)
        if f is None or f.shape != (3, 3) or mask is None or mask.sum() < 35:
            raise ValueError("Cannot estimate consistent stereo geometry; keep both cameras still")
        fit_a, fit_b = a[~hold][mask.ravel() != 0], b[~hold][mask.ravel() != 0]
        # Broad image support avoids accepting a tiny moving patch as the camera rig.
        for pts in (fit_a, fit_b):
            if np.any(np.ptp(pts, axis=0) < np.array(self.size)*.3):
                raise ValueError("Matched features are too concentrated; include more of the surrounding scene")
        ok, h1, h2 = cv2.stereoRectifyUncalibrated(fit_a, fit_b, f, self.size, threshold=3)
        if not ok or h1 is None or h2 is None or not np.isfinite([h1, h2]).all():
            raise ValueError("Natural-feature rectification failed")
        aa = cv2.perspectiveTransform(a[hold, None], h1)[:, 0]
        bb = cv2.perspectiveTransform(b[hold, None], h2)[:, 0]
        dy = np.abs(aa[:, 1]-bb[:, 1])
        good = np.isfinite(dy) & (dy < 2)
        if good.sum() < 10 or good.mean() < .65:
            raise ValueError("Alignment fails held-out matches; improve overlap/lighting and keep scene still")
        p95 = float(np.percentile(dy[good], 95))
        white = np.full((self.size[1], self.size[0]), 255, np.uint8)
        support = [cv2.warpPerspective(white, h, self.size, flags=cv2.INTER_LINEAR) == 255 for h in (h1, h2)]
        if np.mean(support[0] & support[1]) < .4:
            raise ValueError("Rectification loses too much image overlap; make camera views more parallel")
        # Reject extreme homographies that collapse image content into a narrow strip.
        for h in (h1, h2):
            centre = np.float32([[[self.size[0]/2, self.size[1]/2], [self.size[0]/2+20, self.size[1]/2],
                                  [self.size[0]/2, self.size[1]/2+20]]])
            q = cv2.perspectiveTransform(centre, h)[0]
            scales = np.linalg.norm(q[1:]-q[0], axis=1)/20
            if np.any(scales < .4) or np.any(scales > 2.5):
                raise ValueError("Unstable perspective geometry; use a scene with objects at different distances")
        delta = aa[good, 0]-bb[good, 0]
        lo, hi = np.percentile(delta, (2, 98))
        minimum = int(np.floor(lo-24))
        count = int(np.ceil((hi-lo+64)/16))*16
        count = max(64, min(192, count))
        if minimum < -192 or minimum+count > self.size[0]-10:
            raise ValueError("Disparity search outside useful image area; improve camera overlap")
        self._h = (h1, h2)
        self._support = [cv2.erode(s.astype(np.uint8), np.ones((7, 7), np.uint8),
                                  borderType=cv2.BORDER_CONSTANT, borderValue=0).astype(bool) for s in support]
        self.minimum, self.count = minimum, count
        self.right_min = -minimum-count+1
        self._matchers = [cv2.StereoSGBM_create(minDisparity=d, numDisparities=count, blockSize=5,
            P1=8*25, P2=32*25, uniquenessRatio=12, speckleWindowSize=80, speckleRange=2,
            disp12MaxDiff=-1, mode=cv2.STEREO_SGBM_MODE_SGBM) for d in (minimum, self.right_min)]
        self.color_limits = (float(lo-8), float(max(hi+8, lo+16)))
        self.status = f"Aligned: {len(fit_a)} matches, held-out row error p95 {p95:.2f}px"
        self.ready = True
        return self.status

    def process(self, left, right):
        if not self.ready:
            raise RuntimeError("Align before computing experimental disparity")
        start = time.perf_counter()
        l, r = [cv2.warpPerspective(self._gray(im), h, self.size) for im, h in zip((left, right), self._h)]
        dl = self._matchers[0].compute(l, r).astype(np.float32)/16
        dr = self._matchers[1].compute(r, l).astype(np.float32)/16
        x = np.arange(self.size[0])[None, :]
        y = np.arange(self.size[1])[:, None]
        xr = np.rint(x-dl).astype(np.int32)
        inside = (xr >= 0) & (xr < self.size[0])
        ix = np.clip(xr, 0, self.size[0]-1)
        rd = dr[y, ix]
        valid = self._support[0] & self._support[1][y, ix] & inside
        valid &= (dl > self.minimum-1) & (rd > self.right_min-1) & (np.abs(dl+rd) < 1.5)
        # Do not color plausible-looking disparities on textureless walls.
        fl = l.astype(np.float32)
        variance = cv2.boxFilter(fl*fl, -1, (9, 9))-cv2.boxFilter(fl, -1, (9, 9))**2
        valid &= variance > 20
        dl[~valid] = np.nan
        return DisparityResult(l, r, dl, valid, (time.perf_counter()-start)*1000)

    def colorize(self, result):
        lo, hi = self.color_limits
        gray = np.zeros(result.valid.shape, np.uint8)
        gray[result.valid] = np.clip((result.disparity_px[result.valid]-lo)/(hi-lo)*255, 0, 255).astype(np.uint8)
        out = cv2.applyColorMap(gray, cv2.COLORMAP_TURBO)
        out[~result.valid] = 0
        return out


def display_frame(left, right, engine, result, message):
    size = engine.size
    a, b = [cv2.resize(im, size) for im in (left, right)]
    rectified = cv2.cvtColor(result.left, cv2.COLOR_GRAY2BGR) if result else np.zeros_like(a)
    heatmap = engine.colorize(result) if result else np.zeros_like(a)
    tiles = [a, b, rectified, heatmap]
    labels = ["LEFT CAMERA", "RIGHT CAMERA", "RECTIFIED LEFT", "EXPERIMENTAL DISPARITY - NOT METRES"]
    for tile, label in zip(tiles, labels):
        cv2.rectangle(tile, (0, 0), (size[0]-1, 29), (18, 18, 18), -1)
        cv2.putText(tile, label, (8, 20), cv2.FONT_HERSHEY_SIMPLEX, .46, (255, 255, 255), 1, cv2.LINE_AA)
    canvas = np.vstack((np.hstack(tiles[:2]), np.hstack(tiles[2:])))
    footer = np.full((100, size[0]*2, 3), 18, np.uint8)
    lines = [message[:112],
             (f"{result.processing_ms:.0f}ms processing | {np.mean(result.valid):.0%} valid pixels | " if result else "") + "Black = no reliable match",
             "R: re-align after moving cameras | Q/ESC: close | Color = signed pixel disparity, not distance"]
    for i, line in enumerate(lines):
        cv2.putText(footer, line, (8, 23+i*30), cv2.FONT_HERSHEY_SIMPLEX, .48, (220, 220, 220), 1, cv2.LINE_AA)
    return np.vstack((canvas, footer))


def main():
    from .windows import WindowsStereo
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--left", type=int, default=1)
    parser.add_argument("--right", type=int, default=2)
    parser.add_argument("--frames", type=int, default=0, help="0: run until closed")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--snapshot", type=Path, help="Save final diagnostic image locally")
    args = parser.parse_args()
    if args.frames < 0 or (args.headless and args.frames == 0):
        parser.error("Headless mode requires a positive --frames")
    engine = ExperimentalStereo()
    title = "Live stereo - experimental disparity (no calibration / no metres)"
    if not args.headless:
        cv2.namedWindow(title)
    count, last_attempt, last_log = 0, 0., 0.
    result = None
    message = engine.status
    try:
        with WindowsStereo(args.left, args.right, max_skew_s=.025) as source:
            while True:
                left, right = source.read(timeout=10 if count == 0 else 4)
                count += 1
                now = time.monotonic()
                if not engine.ready and now-last_attempt > 2:
                    last_attempt = now
                    try:
                        message = engine.align(left.image, right.image)
                        print(message, flush=True)
                    except (ValueError, cv2.error) as exc:
                        message = str(exc)
                        print("Alignment pending: " + message, flush=True)
                result = engine.process(left.image, right.image) if engine.ready else None
                canvas = display_frame(left.image, right.image, engine, result, message)
                if result and now-last_log > 5:
                    print(f"Live disparity: {np.mean(result.valid):.1%} valid, {result.processing_ms:.1f}ms processing", flush=True)
                    last_log = now
                if not args.headless:
                    cv2.imshow(title, canvas)
                    key = cv2.waitKey(1) & 0xff
                    if key in (27, ord('q')) or cv2.getWindowProperty(title, cv2.WND_PROP_VISIBLE) < 1:
                        break
                    if key == ord('r'):
                        engine.reset()
                        result = None
                        message = engine.status
                        last_attempt = 0
                if args.frames and count >= args.frames:
                    break
    finally:
        if not args.headless:
            cv2.destroyAllWindows()
    if args.snapshot and count:
        args.snapshot.parent.mkdir(parents=True, exist_ok=True)
        if not cv2.imwrite(str(args.snapshot), canvas):
            raise OSError("Could not write diagnostic snapshot")
    print(f"Processed {count} pairs; alignment ready={engine.ready}; no metric depth claimed", flush=True)


if __name__ == "__main__":
    main()
