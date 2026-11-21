# Turret

Computer-vision turret: a Logitech C270 on a Raspberry Pi 5 (QNX 8.0) streams video to a laptop, which tracks a
colored target and aims a two-servo gimbal (ESP32). Phase 1: working tracking + aiming + dashboard.

```
C270 -> Pi (QNX) --SSH/Wi-Fi--> laptop --USB serial (COM7) or UDP--> ESP32 -> pan/tilt servos
                                  |
                                  +--> dashboard (http://localhost:8000)
```

## Run
1. Windows hotspot on (name/password in your wifi_config.h); Pi powered and joined; ESP32 plugged into the laptop (COM7).
2. `python laptop/app.py`, then open http://localhost:8000.
3. Click **Teal** (or click the target), then **Tracking: ON**.

Settings: `GIMBAL` env var picks the ESP link (`COM7` default, or `udp:192.168.137.50`); `PI_HOST` overrides the
Pi address (normally found by its MAC address, because the hotspot changes its IP).

## Layout
- `pi/pi_stream.py` (copy to `~/bin/` on the Pi): captures with QNX's `camera_example4_record`, JPEG-encodes the
  newest 640x480 frame with libturbojpeg, streams it to stdout.
- `laptop/receiver.py` SSH frame receiver; `tracker.py` color tracker (joins fragments, learns size);
  `aim.py` position -> servo angles (Map mode: picture maps linearly onto servo travel; Follow mode: camera on gimbal);
  `gimbal.py` ESP driver (serial or UDP); `app.py` + `dashboard.html` server and UI (mirror/flip, presets, modes).
- `esp/gimbal_wifi/` firmware (USB serial + Wi-Fi UDP, same commands as the original `gimbal.ino`);
  copy `wifi_config.example.h` to `wifi_config.h`. `esp/wifi_diag/` is a Wi-Fi scan/diagnostic sketch.
- `tests/` run with `python tests/test_aim.py` (also `test_tracker.py`, `test_gimbal.py`); no hardware needed.

## Hardware notes
- ESP32 commands: `P<0-180>` pan (GPIO 19), `T<90-180>` tilt (GPIO 18), `C` center (90/135), `S` status. Servos need their
  own supply with a ground shared with the ESP.
- QNX camera: unit 5, 640x480 YUY2; recorded-file frames are 614408 bytes apart (640*480*2 + 8), not 614400.
  Frame rate is 13-28 fps depending on the camera/light. `shutdown -b` halts the Pi (power-cycle by hand).
- Compile/upload the ESP from the command line with the `arduino-cli` bundled in the Arduino IDE (board `esp32:esp32:esp32`,
  upload speed 115200 is the reliable one).
