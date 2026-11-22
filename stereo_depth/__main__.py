"""python -m stereo_depth {calibrate,replay,live,demo}. No implicit camera opening."""
import argparse
import importlib
import json
from pathlib import Path
import time

import cv2
import numpy as np

from .core import Calibration, Config, StereoDepth
from .calibrate import calibrate_manifest, read_manifest
from .view import panel


def synthetic_pair(width=320, height=240, disparity=12, seed=123):
    rng = np.random.default_rng(seed)
    left = rng.integers(0, 256, (height, width), np.uint8)
    right = rng.integers(0, 256, (height, width), np.uint8)
    right[:, :width-disparity] = left[:, disparity:]
    k = np.array([[240., 0, width/2], [0, 240., height/2], [0, 0, 1.]])
    cal = Calibration((width, height), k, np.zeros(5), k, np.zeros(5), np.eye(3),
                      np.array([-0.1, 0, 0]), 0., 0., "synthetic-left", "synthetic-right")
    return left, right, cal


def save_result(result, directory, index):
    directory.mkdir(parents=True, exist_ok=True)
    stem = directory / f"{index:06d}"
    np.savez_compressed(str(stem)+".npz", depth_m=result.depth_m, disparity_px=result.disparity_px,
                        valid=result.valid, intrinsics=result.intrinsics)
    if not cv2.imwrite(str(stem)+".png", panel(result)):
        raise OSError("Could not write preview")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    cal = sub.add_parser("calibrate")
    cal.add_argument("manifest")
    cal.add_argument("--board", nargs=2, type=int, default=(9, 6), metavar=("COLS", "ROWS"))
    cal.add_argument("--square-m", type=float, required=True)
    cal.add_argument("--baseline-m", type=float)
    cal.add_argument("--static-pairs", action="store_true")
    cal.add_argument("--max-skew-ms", type=float, default=15)
    cal.add_argument("--output", required=True)
    for name in ("demo", "replay", "live"):
        p = sub.add_parser(name)
        p.add_argument("--algorithm", choices=("bm", "sgbm"), default="bm")
        p.add_argument("--size", type=int, nargs=2, default=(320, 240))
        p.add_argument("--disparities", type=int, default=64)
        p.add_argument("--show", action="store_true", help="Requires GUI-enabled OpenCV; never needed on Pi")
        p.add_argument("--output", type=Path, help="Optional per-frame files; adds I/O latency")
        if name == "demo":
            p.add_argument("--frames", type=int, default=100)
        else:
            p.add_argument("--calibration", required=True)
            if name == "replay":
                p.add_argument("--manifest", required=True)
            else:
                p.add_argument("--source", required=True, help="module:factory yielding pairs of stereo_depth.Frame from your existing feeds")
    args = parser.parse_args()
    if args.command == "calibrate":
        calibration, report = calibrate_manifest(args.manifest, tuple(args.board), args.square_m,
            static_pairs=args.static_pairs, max_skew_s=args.max_skew_ms/1000, expected_baseline_m=args.baseline_m)
        calibration.save(args.output)
        Path(args.output+".json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2))
        return
    config = Config(output_size=tuple(args.size), algorithm=args.algorithm, num_disparities=args.disparities)
    if args.command == "demo":
        if args.frames < 1:
            parser.error("--frames must be positive")
        l, r, calibration = synthetic_pair(*args.size)
        pairs = ((l, r, None, None) for _ in range(args.frames))
    else:
        calibration = Calibration.load(args.calibration)
        if args.command == "replay":
            data = read_manifest(args.manifest)
            if (data["left_id"], data["right_id"]) != (calibration.left_id, calibration.right_id):
                raise ValueError("Manifest camera identities do not match calibration")
            pairs = ((cv2.imread(p["left"]), cv2.imread(p["right"]), p.get("timestamps_s"), p.get("clock_domain")) for p in data["pairs"])
        else:
            module, factory = args.source.split(":", 1)
            source = getattr(importlib.import_module(module), factory)()
            def received():
                try:
                    for l, r in source:
                        if (l.camera_id, r.camera_id) != (calibration.left_id, calibration.right_id):
                            raise ValueError("Source camera identities do not match calibration")
                        if not l.clock_domain or l.clock_domain != r.clock_domain:
                            raise ValueError("Source clocks are not shared")
                        yield l.image, r.image, (l.timestamp_s, r.timestamp_s), l.clock_domain
                finally:
                    if hasattr(source, "close"):
                        source.close()
            pairs = received()
    processor = StereoDepth(calibration, config)
    durations, fractions = [], []
    start = time.perf_counter()
    count = 0
    try:
        for count, (l, r, ts, domain) in enumerate(pairs, 1):
            result = processor.process(l, r, timestamps_s=ts, clock_domain=domain)
            durations.append(result.processing_ms)
            fractions.append(result.valid_fraction)
            # Bound long-running live telemetry storage.
            if len(durations) > 1000:
                del durations[0]
                del fractions[0]
            if args.output:
                save_result(result, args.output, count)
            if args.show:
                cv2.imshow("Stereo: rectified left | depth (black = invalid)", panel(result))
                if cv2.waitKey(1) & 0xff in (27, ord('q')):
                    break
    finally:
        if hasattr(pairs, "close"):
            pairs.close()
        if args.show:
            cv2.destroyAllWindows()
    print(json.dumps({"mode": args.command, "frames": count, "algorithm": args.algorithm,
        "elapsed_s": time.perf_counter()-start,
        "processing_p50_ms": float(np.median(durations)) if durations else None,
        "processing_p95_ms": float(np.percentile(durations, 95)) if durations else None,
        "mean_valid_fraction": float(np.mean(fractions)) if fractions else None,
        "note": "Synthetic/replay timing is not a QNX hardware benchmark."}, indent=2))


if __name__ == "__main__":
    main()
