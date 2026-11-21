"""Receive existing frames without starting, stopping, or reconfiguring cameras."""
import numpy as np


def yuy2_gray(buffer, width, height, stride=None):
    """Zero-copy YUY2 luminance view, including padded rows.

    The capture buffer must remain valid until synchronous process returns.
    FramePairer/StereoWorker copy inputs before returning to the capture owner.
    """
    if not isinstance(width, int) or not isinstance(height, int) or width <= 0 or height <= 0 or width % 2:
        raise ValueError("YUY2 requires positive dimensions and even width")
    stride = 2*width if stride is None else stride
    if not isinstance(stride, int) or stride < 2*width:
        raise ValueError("Invalid row stride")
    if memoryview(buffer).nbytes < (height-1)*stride+2*width:
        raise ValueError("Truncated YUY2 buffer")
    return np.ndarray((height, width), dtype=np.uint8, buffer=buffer, strides=(stride, 2))
