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
3. Click **Teal** (or click the target), then **Tracking: ON**. "Show color match" tints the pixels the filter accepts.

The Pi's SSH key is pinned under the alias `qnxpi` in `~/.ssh/known_hosts` (so a new IP address needs no re-trusting).
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
- `tests/` need no hardware: `test_aim.py`, `test_tracker.py`, `test_gimbal.py`, `test_feed.py`, `test_latency_tool.py`,
  `test_predict.py`, `test_engage.py`, `test_telemetry.py`, `test_server.py` (HTTP endpoints), `node tests/dashboard_smoke.js`
  (dashboard script against sample data), and `test_pipeline.py` (synthetic camera frames through flip -> tracker -> aim ->
  async gimbal -> simulated ESP, including the state machine). Run them all: `python tests/run_all.py`.
- `tools/latency_test.py` measures glass-to-glass camera latency (stop the server first; aim the camera at the screen).

## ToF distance sensor (VL53L0X / VL53L1X)
Wiring: VIN -> 3V3, GND -> GND, SDA -> GPIO 23, SCL -> GPIO 22 (leave XSHUT/GPIO1 open; avoid GPIO 12, a boot-strapping pin).
`esp/gimbal_wifi/tof.h` auto-detects the model and reads it without ever blocking the servo code; no sensor means the
firmware behaves as before. Commands: `R` -> `R<mm>` (`R-1` = none), `I` -> sensor model. `esp/tof_probe` is a read-only
wiring check; `python tools/tof_monitor.py` shows a live reading. The sensor is static for now (not on the gimbal), so
distance is displayed only; "target confirmed" logic comes once it rides on the bracket.

## Mission control and telemetry
`laptop/engage.py` is the engagement state machine: IDLE -> SEARCH -> ACQUIRE -> TRACK -> LOCK -> (fire) -> COOLDOWN.
Fire interlocks, all required: master **ARM** on (starts off; switching tracking off disarms), tracking on, target seen
continuously, gimbal settled on it for 8 frames, target slower than a limit, lock held 0.5 s, cooldown (1.5 s) over, and
under the shot cap (30 since arming). The emitter is simulated (it only logs) until the IR hardware exists: plug the real
one in by giving `Engagement` an object with `fire(ts)`. Optional **auto-scan** sweeps the pan 50-130 degrees while searching.
`laptop/telemetry.py` keeps per-frame timing (frame wait, processing, interval), servo angles, target speed and the ESP's
command round trip in ring buffers; the dashboard charts them live with p50/p95/max numbers and an event log.

## Motion prediction
`laptop/predict.py` keeps a smoothed position and velocity for the target (alpha-beta filter) and the turret aims
`lead_ms` ahead of it (slider on the dashboard; **off by default**: speed estimates are noisy and the lead turned pixel noise
into servo jitter. Raise it only to about the measured camera-to-servo delay). A yellow
dot shows the aim point. In simulation (target seen 250 ms late, 15 fps, noise) the aim error falls from 23 to 3 px at
constant speed, 29 to 10 px for stop-and-go, and 84 to 60 px for a fast swing; a still target does not jitter more.
The true delay has not been measured on the hardware yet: use `tools/latency_test.py`, then set the slider to match.

## Neural depth layer (optional, off by default)
`laptop/depth_nn.py` estimates dense RELATIVE depth from one camera with Depth Anything V2 Small (ONNX, CPU, Apache-2.0):
about 5-7 fps on a laptop CPU at 308x224, on its own thread so the video is not slowed. It is an estimate, not a measurement,
so it is shown as a see-through overlay labelled "estimated, relative". Switch it on with the dashboard's "Depth (estimated)"
button. It runs on the clean picture AFTER the teal tracker has found the target, so it cannot disturb tracking.
Setup: `pip install onnxruntime`, and download `onnx/model.onnx` from `onnx-community/depth-anything-v2-small` on Hugging Face
to `models/depth_model.onnx` (about 94 MB; `models/` is git-ignored). Without them the layer just reports why it cannot start.
Try it on a laptop camera: `python tools/depth_live.py --camera 1`.

## Latency design
- `receiver.FrameFeed` reads the Pi stream on its own thread and keeps only the newest frame (stale frames are dropped, not
  queued). The dashboard shows how long each frame waited and how many were dropped.
- `gimbal.AsyncGimbal` sends servo commands from a background thread (latest angle per axis wins), so the ESP's ~40 ms per
  move never blocks the video loop. Measured laptop processing (decode, flip, track, aim, encode): about 3 ms per frame.
- Resolved: P/T commands used to take exactly 40 ms for the ESP to answer (status took 5 ms), even when sending the pulse
  width directly. After the gimbal was rewired they take 4-5 ms, so the delay was electrical (wiring/power), not firmware.

## Hardware notes
- ESP32 commands: `P<0-180>` pan (GPIO 19), `T<90-180>` tilt (GPIO 18), `C` center (90/135), `S` status. Servos need their
  own supply with a ground shared with the ESP.
- QNX camera: unit 5, 640x480 YUY2; recorded-file frames are 614408 bytes apart (640*480*2 + 8), not 614400.
  Frame rate is 13-28 fps depending on the camera/light. `shutdown -b` halts the Pi (power-cycle by hand).
- Compile/upload the ESP from the command line with the `arduino-cli` bundled in the Arduino IDE (board `esp32:esp32:esp32`,
  upload speed 115200 is the reliable one).
