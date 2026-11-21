"""Tests the distance-sensor driver calls and the monitor tool with a simulated ESP. Run: python tests/test_tof.py"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "laptop"))
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
from gimbal import AsyncGimbal, Gimbal, GimbalError   # noqa: E402
import tof_monitor   # noqa: E402


class ToFESP:
    """Answers like the firmware: R -> R<mm> (or R-1), I -> I <model>; anything else as a normal status."""

    def __init__(self, model="VL53L1X", mm=1234):
        self.model, self.mm, self.out = model, mm, b""

    def write(self, data):
        cmd = data.decode().strip()
        if cmd == "R":
            self.out = f"R{self.mm if self.model else -1}\r\n".encode()
        elif cmd == "I":
            self.out = f"I {self.model or 'none'}\r\n".encode()
        else:
            self.out = b"P90 T135\r\n"

    def readline(self):
        line, self.out = self.out, b""
        return line

    def close(self):
        pass


class OldFirmware(ToFESP):
    def write(self, data):
        self.out = b"ERR unknown command\r\n"


def test():
    g = Gimbal(link=ToFESP("VL53L1X", 1234))
    assert g.sensor_model() == "VL53L1X" and g.distance_mm() == 1234
    assert Gimbal(link=ToFESP("VL53L0X", 480)).sensor_model() == "VL53L0X"

    g = Gimbal(link=ToFESP(None))                               # no sensor wired: handled, not an error
    assert g.sensor_model() is None and g.distance_mm() is None

    g = Gimbal(link=ToFESP("VL53L1X", -1))                      # sensor present but nothing valid in range
    assert g.distance_mm() is None

    g = Gimbal(link=OldFirmware())                              # old firmware without the commands: a clear error
    try:
        g.distance_mm()
        raise SystemExit("expected GimbalError from firmware without R")
    except GimbalError as e:
        assert "unexpected reply" in str(e)

    ag = AsyncGimbal(link=ToFESP("VL53L1X", 777))
    assert ag.distance_mm() == 777
    ag.close()

    hist = []                                                   # the monitor's status line
    line = tof_monitor.format_reading(1000, hist)
    assert "1000 mm" in line and "min 1000" in line and line.count("#") == 20
    tof_monitor.format_reading(500, hist)
    line = tof_monitor.format_reading(1500, hist)
    assert "min 500" in line and "max 1500" in line and "mean 1000" in line
    assert "no reading" in tof_monitor.format_reading(None, hist) and len(hist) == 3
    assert tof_monitor.format_reading(5000, [] ).count("#") == 40, "bar is capped"
    print("ToF driver tests passed")


if __name__ == "__main__":
    test()
