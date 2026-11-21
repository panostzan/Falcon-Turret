"""Optional display/export helpers. No GUI imports or windows in the processing path."""
import cv2
import numpy as np


def colorize(result, near_m=0.2, far_m=5.0):
    if not np.isfinite([near_m, far_m]).all() or not 0 < near_m < far_m:
        raise ValueError("Require 0 < near_m < far_m")
    normalized = np.zeros(result.depth_m.shape, np.uint8)
    normalized[result.valid] = np.clip(255*(far_m-result.depth_m[result.valid])/(far_m-near_m), 0, 255).astype(np.uint8)
    image = cv2.applyColorMap(normalized, cv2.COLORMAP_TURBO)
    image[~result.valid] = 0
    return image


def panel(result):
    image = np.hstack((cv2.cvtColor(result.rectified_left, cv2.COLOR_GRAY2BGR), colorize(result)))
    timing = "skew unknown" if result.skew_s is None else f"skew {result.skew_s*1000:.1f}ms"
    cv2.putText(image, f"{result.processing_ms:.1f}ms | {result.valid_fraction:.0%} valid | {timing}",
                (8, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (255, 255, 255), 1, cv2.LINE_AA)
    return image
