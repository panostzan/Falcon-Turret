# TURRET

### A real-time autonomous turret on QNX. It finds its target, aims, and refuses to fire at a human, and the timing is guaranteed.

A Raspberry Pi 5 running **QNX 8.0** does the time-critical work in native C: colour tracking, the mission state machine and the
fire interlock. An on-device **TensorFlow Lite** face detector, ported to QNX, vetoes any shot when a person is in frame.
A laptop turns the Pi's answers into servo angles for a four-axis mechanical turret with a retractable roof.

> **Firing is simulated.** The laser did not arrive in time, so a "shot" is logged and shown on the dashboard and nothing is
> emitted. Everything upstream of the trigger (tracking, lock, interlock, face veto) is real and running.

| | |
|---|---|
| **OS** | QNX 8.0 (microkernel RTOS) on Raspberry Pi 5, 4x Cortex-A76 |
| **Tracker** | native C, about 9 ms per 640x480 frame |
| **Camera** | USB webcam, about 15 fps, raw YUY2 straight into the tracker |
| **AI** | BlazeFace via QNX's TensorFlow Lite port, own process, pinned to one core |
| **Link** | tracking results over UDP, video over SSH for display only |
| **Actuators** | pan, tilt, platform lift, roof: Feather M0 + PCA9685 |
| **Tests** | about 36 suites, none need hardware |

## The idea: late data is wrong data

A turret that aims at where the target *was* is broken, however fast it is on average. So the question is not "how fast is the
tracker?" but **"does it finish before the next frame, every time, even when the machine is on fire?"** That is what a real-time
OS is for. Real-time means *on time*, not just fast.

```
 RASPBERRY PI 5 . QNX 8.0                              LAPTOP . Windows
 ----------------------------------------------        ------------------------------
 USB webcam --raw YUY2--> turretd  (prio 30)           dashboard + aim.py
                          . colour tracker (C)           target position -> servo angles
                          . state machine + interlock    . prediction (lead the target)
                          . watchdog                     . roof / platform logic
                              |   ^                      . manual Servos page
              UDP results ----+   | human 0|1                     |
                                  |                       USB serial (COM8)
 facewatch (prio 10)  -----------+                                |
   TensorFlow Lite on QNX                               Feather M0 -> PCA9685 -> servos
   BlazeFace, 1 core, ~3 Hz                                pan . tilt . platform . roof
                                                                  
 pi_stream.py (prio 20) --JPEG over SSH--> dashboard (display only, never used for aiming)
```

## How QNX is used (the part that matters)

| Priority | Process | Why it sits there |
|---|---|---|
| **30** | `turretd`: tracker, state machine, interlock, watchdog | the deadline-critical path; nothing may delay it |
| **20** | `pi_stream.py`: video and network | important, but a dropped frame is fine |
| **10** | `facewatch` (the AI) and everything else | background work must never steal time from the tracker |

- **Priority-based preemptive scheduling.** The higher number always runs first, so a CPU-hungry neural net cannot make the
  tracker miss a frame.
- **Isolation by process (microkernel thinking).** The AI is its own program. If it crashes or stalls, `turretd` simply stops
  hearing from it and the human gate **refuses to fire**. A supervisor restarts it, and backs off after repeated crashes so a
  crash loop cannot eat the CPU.
- **Native QNX services.** Camera through the QNX sensor framework (`camera_example4_record`, unit 5), `libturbojpeg` for JPEGs,
  `pidin` / `top` / `usb` for the live system view, POSIX `poll()` with `CLOCK_MONOTONIC` for a loop that watches both the
  camera and the control socket.
- **Proof, not a claim.** The dashboard's **QNX page** has an overload demo: it launches CPU hogs at priority 10, then compares
  the tracker at priority 30 (stays under the 30 ms deadline line) against priority 10 (misses it).

## Safety: it has to be hard to fire

A shot needs **every one** of these at once. This logic runs on the Pi, in C (`qnx/engage.c`).

```
IDLE -> SEARCH -> ACQUIRE -> TRACK -> LOCK -> (FIRE) -> COOLDOWN
        target   3 frames   8 settled   0.5 s
        seen     seen       frames      dwell
```

- Master **ARM** is on (starts off; arming resets the shot counter; the UI needs a 0.7 s hold-to-arm)
- Tracking on, target seen continuously, aim settled on it, target slow enough, confidence >= 0.8
- Lock held 0.5 s, cooldown (1.5 s) over, fewer than 30 shots since arming, roof open
- **Human gate:** no face in view, and the AI has reported within 2 s. A **silent AI blocks firing**, so it fails safe.
- **Watchdog:** if the laptop's heartbeat stops for 1.5 s, `turretd` disarms itself, whatever the laptop or network is doing.

The face check does *detection* only (is there a face), never recognition. It is off by default and switched on from the dashboard.

## Latency engineering

- **Newest frame only.** Frames are never queued. The receiver keeps the latest and drops the rest, and counts what it dropped.
- **Aim on arrival.** The laptop aims the instant a tracker answer arrives, not on the next video frame.
- **UDP for results.** A late packet is worthless, so there are no retransmit waits.
- **Async servo I/O.** Servo commands go out from a background thread (latest angle per axis wins), so the serial port never
  blocks the video loop. Laptop processing is about 3 ms per frame.
- **Prediction.** An alpha-beta filter leads the target and keeps following its path through brief occlusions.
- **Measured, per stage.** The Diagnostics page charts frame wait, processing and servo round trip with p50 / p95 / max.
  Wi-Fi is the one thing QNX cannot guarantee (idle hotspot pings: roughly 12 to 114 ms), so it is measured, not hidden.

## The mechanical turret

- **Four axes:** pan, tilt, platform lift and a retractable **roof**, driven through a PCA9685.
- **Dormant / Active:** dormant means roof closed and platform down. When a target appears the roof opens and the platform
  follows after a short stagger; after 10 s without a target it closes again. **Hibernate** locks it shut until released.
- **Collision-proof:** the platform cannot rise while the roof is closed and the roof cannot close onto a raised platform, in
  both the automatic and the manual logic. Roof moves use a smooth glide to reduce stress on the servo.
- **Manual mode:** the Servos page has four rate-limited sliders. "Back to Auto" parks everything in a safe order before it
  releases the serial port.

## Also in there

- **Colour tracking** with teal / blue presets or click-to-pick, contour joining, learned size and a "show color match" overlay
- **Estimated depth** from a single webcam (Depth Anything V2 Small, ONNX), shown as an overlay labelled "estimated, relative"
- **Trails**, live **diagnostics**, an event log, and a system check page

## Run

1. Windows hotspot on; Pi powered and joined; Feather M0 on the laptop (COM8); servo supply on the PCA9685.
2. `python laptop/app.py` (or `python launch.py`, or `Turret.bat`), then open http://localhost:8000
3. Click **Teal** (or click the target), then **Tracking: ON**.

`GIMBAL` picks the serial port (default `COM8`), `PI_HOST` overrides the Pi address (normally found by MAC address because the
hotspot changes its IP). Roof tuning: `TURRET_ROOF_SECONDS`, `TURRET_ROOF_STAGGER`. Manual mode: `TURRET_MANUAL_STEP`.
Tests: `python tests/run_all.py`.

## Repo map

| Path | What |
|---|---|
| `qnx/` | C/C++ for the Pi: `turretd.c`, `tracker.c`, `engage.c`, and `ai/facewatch.cc` with its build scripts |
| `pi/` | Python on the Pi: `pi_stream.py` (camera to JPEG), `aiproc.py` (AI supervisor), `sysmon.py` |
| `laptop/` | dashboard server and driver: `app.py`, `aim.py`, `predict.py`, `roof.py`, `manual.py`, `gimbal.py`, `depth_nn.py`, `ui/` |
| `firmware/feather_m0/` | Feather M0 sketch: serial commands in, I2C to PCA9685 out |
| `tools/` | helpers, e.g. `latency_test.py` for glass-to-glass latency |
| `tests/` | no-hardware tests |
| `esp/` | the original ESP32 firmware, **legacy** |

The laptop keeps its own copy of the tracker and state machine as a fallback for when QNX is not answering. In that mode the
Pi-side priorities and watchdog do not protect anything.

## Honest limits and what's next

- The emitter is simulated (the laser did not arrive in time).
- Aiming runs on the laptop, so the servo path crosses Wi-Fi. **Moving the aim loop onto QNX is the next step.**
- The face model is short-range BlazeFace and detects faces only, not bodies or hands.
- The supplied camera is too heavy to mount on the gimbal; a lighter one would let it ride on the turret.
- Video still travels over SSH/TCP. A UDP video path is possible but should be measured first.
