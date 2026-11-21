"""Live distance readout from the ToF sensor on the ESP.

Run in your own terminal:  python tools\\tof_monitor.py        (or  python tools\\tof_monitor.py udp:192.168.137.50)
Stop the dashboard first: only one program can use the ESP's USB port. Press Ctrl+C to stop.
The ESP must be running the gimbal firmware that includes the distance sensor (command R / I).
"""
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "laptop"))
from gimbal import Gimbal, GimbalError   # noqa: E402

BAR_MM = 2000     # full bar = 2 m


def format_reading(mm, history):
    """One status line: the distance, a bar, and the min / mean / max of the valid readings so far."""
    if mm is None:
        return "no reading (out of range, or no sensor)"
    history.append(mm)
    bar = "#" * min(40, round(40 * mm / BAR_MM))
    return f"{mm:5d} mm |{bar:<40}| min {min(history)}  mean {statistics.fmean(history):.0f}  max {max(history)}"


def main(port="COM7", seconds=None, rate_hz=5):
    with Gimbal(port) as g:
        try:
            model = g.sensor_model()
        except GimbalError as e:
            sys.exit(f"The ESP doesn't know the distance commands ({e}). Load the gimbal firmware that includes the ToF sensor.")
        if model is None:
            sys.exit("The ESP found no distance sensor. Check the wiring: VIN->3V3, GND->GND, SDA->GPIO23, SCL->GPIO22.")
        print(f"sensor: {model}. Ctrl+C to stop.")
        history, t0 = [], time.time()
        try:
            while seconds is None or time.time() - t0 < seconds:
                print(format_reading(g.distance_mm(), history), flush=True)
                time.sleep(1 / rate_hz)
        except KeyboardInterrupt:
            print()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "COM7")
