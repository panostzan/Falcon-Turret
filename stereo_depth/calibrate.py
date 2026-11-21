"""Offline calibration from matched checkerboard images; no capture dependency."""
import json
import math
from pathlib import Path

import cv2
import numpy as np

from .core import Calibration


def read_manifest(path):
    """JSON {left_id,right_id,pairs:[{left,right,...}]} with paths relative to JSON."""
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not data.get("left_id") or not data.get("right_id") or data["left_id"] == data["right_id"]:
        raise ValueError("Manifest needs distinct left_id and right_id")
    if not data.get("pairs"):
        raise ValueError("Manifest contains no pairs")
    for pair in data["pairs"]:
        pair["left"] = str(path.parent / pair["left"])
        pair["right"] = str(path.parent / pair["right"])
    return data


def fit_points(objects, left_points, right_points, image_size, left_id, right_id,
               max_rms_px=1.0, max_epipolar_px=1.0, expected_baseline_m=None):
    """Fit with 20% held out; all object-point coordinates MUST be metres.

    Return (Calibration, diagnostics). Bad fits never produce an accepted file.
    """
    n = len(objects)
    if n < 15 or len(left_points) != n or len(right_points) != n:
        raise ValueError("At least 15 usable paired views are required (20-30 recommended)")
    if any(not math.isfinite(v) or v <= 0 for v in (max_rms_px, max_epipolar_px)):
        raise ValueError("Quality limits must be finite and positive")
    if expected_baseline_m is not None and (not math.isfinite(expected_baseline_m) or expected_baseline_m <= 0):
        raise ValueError("Expected baseline must be finite and positive")
    objects = [np.asarray(v, np.float32).reshape(-1, 3) for v in objects]
    left_points = [np.asarray(v, np.float32).reshape(-1, 1, 2) for v in left_points]
    right_points = [np.asarray(v, np.float32).reshape(-1, 1, 2) for v in right_points]
    for o, l, r in zip(objects, left_points, right_points):
        if len(o) < 12 or len(l) != len(o) or len(r) != len(o) or not all(np.isfinite(a).all() for a in (o, l, r)):
            raise ValueError("Invalid corner observations")
    centres = np.array([p.mean(axis=(0, 1)) for p in left_points])
    if np.any(np.ptp(centres, axis=0) < np.array(image_size)*0.1):
        raise ValueError("Insufficient board position diversity; cover the image and vary board tilt/distance")
    train = [i for i in range(n) if i % 5 != 0]
    held = [i for i in range(n) if i % 5 == 0]
    obj = [objects[i] for i in train]
    lp, rp = [left_points[i] for i in train], [right_points[i] for i in train]
    rms_l, kl, dl, _, _ = cv2.calibrateCamera(obj, lp, image_size, None, None)
    rms_r, kr, dr, _, _ = cv2.calibrateCamera(obj, rp, image_size, None, None)
    rms, kl, dl, kr, dr, rotation, translation, _, _ = cv2.stereoCalibrate(
        obj, lp, rp, kl, dl, kr, dr, image_size, flags=cv2.CALIB_FIX_INTRINSIC,
        criteria=(cv2.TERM_CRITERIA_EPS+cv2.TERM_CRITERIA_MAX_ITER, 100, 1e-7))
    r1, r2, p1, p2, _, _, _ = cv2.stereoRectify(kl, dl, kr, dr, image_size, rotation, translation,
                                              flags=cv2.CALIB_ZERO_DISPARITY, alpha=0)
    errors = []
    for l, r in zip(left_points, right_points):
        a = cv2.undistortPoints(l, kl, dl, R=r1, P=p1).reshape(-1, 2)
        b = cv2.undistortPoints(r, kr, dr, R=r2, P=p2).reshape(-1, 2)
        errors.append(np.abs(a[:, 1]-b[:, 1]))
    held_p95 = float(np.percentile(np.concatenate([errors[i] for i in held]), 95))
    all_p95 = float(np.percentile(np.concatenate(errors), 95))
    quality = max(float(rms), float(rms_l), float(rms_r))
    epipolar = max(held_p95, all_p95)
    if not math.isfinite(quality) or quality > max_rms_px or not math.isfinite(epipolar) or epipolar > max_epipolar_px:
        raise ValueError(f"Calibration rejected: RMS={quality:.3f}px, epipolar p95={epipolar:.3f}px; limits {max_rms_px}/{max_epipolar_px}px")
    cal = Calibration(tuple(image_size), kl, dl, kr, dr, rotation, translation,
                      quality, epipolar, left_id, right_id)
    baseline = float(np.linalg.norm(translation))
    if expected_baseline_m is not None and abs(baseline-expected_baseline_m) > expected_baseline_m*0.1:
        raise ValueError(f"Baseline {baseline:.4f}m differs from measured baseline by >10%; check square units and camera order")
    return cal, {"views": n, "training_views": len(train), "held_out_views": len(held),
                 "rms_left_px": float(rms_l), "rms_right_px": float(rms_r), "rms_stereo_px": float(rms),
                 "held_out_epipolar_p95_px": held_p95, "all_epipolar_p95_px": all_p95,
                 "per_view_epipolar_p95_px": [float(np.percentile(e, 95)) for e in errors],
                 "baseline_m": baseline, "image_size": list(image_size)}


def calibrate_manifest(manifest, board, square_m, *, static_pairs=False, max_skew_s=0.015,
                       expected_baseline_m=None):
    if len(board) != 2 or any(v < 3 for v in board) or not math.isfinite(square_m) or square_m <= 0:
        raise ValueError("Use inner-corner board dimensions >=3 and a positive square size in METRES")
    data = read_manifest(manifest)
    template = np.zeros((board[0]*board[1], 3), np.float32)
    template[:, :2] = np.mgrid[0:board[0], 0:board[1]].T.reshape(-1, 2)*square_m
    objects, lp, rp, skipped = [], [], [], []
    size = None
    for index, pair in enumerate(data["pairs"]):
        ts = pair.get("timestamps_s")
        if ts is None and not static_pairs:
            raise ValueError("Calibration requires timestamps/clock_domain or explicit --static-pairs with a stationary board")
        if ts is not None and (len(ts) != 2 or not all(math.isfinite(t) and t >= 0 for t in ts)
                               or not pair.get("clock_domain") or abs(ts[0]-ts[1]) > max_skew_s):
            raise ValueError(f"Invalid timing or excessive skew in pair {index}")
        l, r = (cv2.imread(pair[k], cv2.IMREAD_GRAYSCALE) for k in ("left", "right"))
        if l is None or r is None:
            raise ValueError(f"Unreadable image in pair {index}")
        if l.shape != r.shape or (size is not None and (l.shape[1], l.shape[0]) != size):
            raise ValueError("All calibration images must have identical dimensions")
        size = (l.shape[1], l.shape[0])
        ok_l, cl = cv2.findChessboardCorners(l, board)
        ok_r, cr = cv2.findChessboardCorners(r, board)
        if not (ok_l and ok_r):
            skipped.append(index)
            continue
        criteria = (cv2.TERM_CRITERIA_EPS+cv2.TERM_CRITERIA_MAX_ITER, 40, 0.001)
        cl = cv2.cornerSubPix(l, cl, (5, 5), (-1, -1), criteria)
        cr = cv2.cornerSubPix(r, cr, (5, 5), (-1, -1), criteria)
        # Opposite detector ordering yields a geometrically false correspondence.
        if float(np.dot((cl[-1]-cl[0]).ravel(), (cr[-1]-cr[0]).ravel())) < 0:
            cr = cr[::-1].copy()
        objects.append(template.copy())
        lp.append(cl)
        rp.append(cr)
    cal, report = fit_points(objects, lp, rp, size, data["left_id"], data["right_id"],
                             expected_baseline_m=expected_baseline_m)
    report.update(skipped_pair_indices=skipped, square_m=square_m, board_inner_corners=list(board),
                  static_pairs_asserted=static_pairs)
    return cal, report
