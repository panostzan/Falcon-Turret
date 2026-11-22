# Stereo depth module

Independent Python/OpenCV processing. Existing camera, tracking and control code is unchanged.

## Verified so far

- 17 numerical/calibration/lifecycle tests pass on Windows (Python 3.13.2, OpenCV 4.13.0, NumPy 2.5.5).
- Both real C270s captured 100 paired 640x480 frames in 5.997 seconds.
- Host-receipt skew p95: 21.0 ms with a 25 ms pairing threshold.
- No physical calibration or ruler-verified metric depth test has been completed.
- QNX deployment remains unverified: both known Pi SSH addresses timed out.

Receipt timestamps are NOT exposure timestamps. Validate stationary scenes first.
Software pairing cannot synchronize these independent webcams.

## Test on Windows, independently of the Pi

From C:\Users\ptzan\turret:

```powershell
python -m stereo_depth.windows preview --left 1 --right 2
python -m stereo_depth.windows check --left 1 --right 2 --frames 100
```

Current DirectShow enumeration: 0 is the built-in camera; 1 and 2 are C270s.
Indices can change. Cover one lens in preview to identify physical left/right;
swap arguments if necessary. Q or Escape closes the viewer and releases cameras.
Close other applications using these webcams. Explicit fallback: --backend msmf.

### Calibration

Rigidly mount both cameras side by side, facing the same direction. Keep their
relative positions fixed. Measure lens-centre separation as a sanity check.
Use a flat checkerboard with 9x6 INNER corners (10x7 squares), measuring the actual
printed square size in metres. 25 mm is 0.025, not 25.

```powershell
python -m stereo_depth.windows record --left 1 --right 2 --output captures/stereo-calibration
python -m stereo_depth calibrate captures/stereo-calibration/manifest.json --board 9 6 --square-m 0.025 --max-skew-ms 25 --output captures/stereo-calibration/pair.npz
```

Space saves when the checkerboard is detected in BOTH views. Capture 20-30 different
poses: cover image edges/centre and vary tilt/distance. Hold the board stationary
during each saved pair. Use a new/empty output directory.
Optional --baseline-m 0.10 checks an approximately 10 cm measured baseline to 10%.

The fitter requires >=15 usable pairs, uses 80% for fitting and 20% for validation,
checks position diversity, and rejects RMS or p95 vertical epipolar errors above 1 px.
It writes calibration plus a JSON quality report. Good fitting error cannot prove
physical scale: verify depth against measured distances.

### Live depth

```powershell
python -m stereo_depth.windows depth --left 1 --right 2 --calibration captures/stereo-calibration/pair.npz
```

Left panel: rectified left image. Right: depth (warm near, cool far, black invalid).
Click either panel to print median valid depth near that rectified coordinate.
Try --algorithm sgbm for more expensive matching. No guessed calibration is used.
Compare textured, stationary targets at measured distances before testing motion.

## Minimal integration

Copy stereo_depth/ beside your program or add its parent to Python's import path.
Runtime dependencies: NumPy and OpenCV core/imgproc/calib3d. No ROS, SciPy,
contrib/ximgproc, neural models, GUI or camera driver required by the processor.

```python
from stereo_depth import StereoDepth

stereo = StereoDepth("pair.npz")  # once
result = stereo.process(left_frame, right_frame)
depth = result.depth_m
distance = result.distance_at(160, 120)  # metres or None
```

Input: original UNMIRRORED uint8 gray or BGR images at calibration resolution.
The existing app mirrors its dashboard: take stereo input BEFORE that transform.
Wrong input dimensions are rejected, not silently resized.
Without timestamps timing is marked unknown; use that mode only for offline/static tests.

```python
result = stereo.process(left_frame, right_frame,
    timestamps_s=(lt, rt), clock_domain="pi-monotonic")
```

Timestamps must be seconds from ONE shared clock. Excessive skew raises ValueError.
Default skew limit is 15 ms; the Windows test adapter explicitly uses 25 ms for host
receipt timestamps. Neither setting proves synchronized exposure.

### Non-blocking integration

```python
from stereo_depth import StereoDepth, StereoWorker
worker = StereoWorker(StereoDepth("pair.npz"))
worker.submit(left_frame, right_frame, timestamps_s=(lt, rt), clock_domain="pi-monotonic")
result = worker.latest()  # None until ready or stale/error
# At shutdown:
worker.close()
```

submit copies images but does not wait for depth processing. At most one pair is
processing and one pending; newest submission replaces pending work. Results expire
200 ms after LOCAL submission, not remote exposure. Transport freshness is the feed's
responsibility. worker.error and worker.dropped expose failure/overload.
Output arrays are independent read-only snapshots; retaining them consumes caller memory.

For separate callbacks, FramePairer(left_id,right_id).push(side,Frame(...)) returns a
pair or None. It enforces identities/common clocks, rejects duplicate or out-of-order
timestamps, bounds queues to three frames per camera, expires unmatched waits at
200 ms, and never reuses a frame. Recreate the pairer after capture-clock reset.

## Output contract

| Field | Meaning |
|---|---|
| depth_m | float32 optical-axis Z in metres; NaN invalid; not Euclidean range |
| disparity_px | float32 disparity in OUTPUT pixels; NaN invalid |
| valid | Boolean validity mask |
| rectified_left | Gray image defining output coordinates |
| intrinsics | Rectified output 3x3 camera matrix |
| distance_at(x,y,radius=0) | Z or None; radius >0 uses median valid neighbours |
| timestamps_s / clock_domain / skew_s | Supplied timing, or None |
| timing_known | Timestamp skew known, NOT exposure synchronization verified |
| processing_ms / completed_monotonic_s | Local processing duration/completion |
| sequence / frame_id / valid_fraction | Result number, camera frame, coverage |

Coordinates refer to rectified output, NOT raw input or mirrored dashboard pixels.
Future XYZ reconstruction: X=(u-cx)*Z/fx, Y=(v-cy)*Z/fy.

Default: 320x240 output, 64 disparities, 9x9 StereoBM. Rectification maps are cached.
Independent right matching checks left/right consistency. Unsupported borders,
nonpositive disparities, speckles, inconsistent matches and out-of-range depth
are invalidated. Invalid values are never filled with fabricated distances.
Textureless/repeating/shiny surfaces and moving scenes can still defeat matching.
Validity is not a calibrated probability/confidence guarantee.

Depth uses Z=(-P2[0,3])/(d-(cx_left-cx_right)). Output resizing recomputes geometry.
Disparity search also limits nearest depth, roughly fx*baseline/(D-1); configured
0.15-8 m bounds do not promise accuracy/coverage throughout that range.
No dense XYZ cloud or temporal history by default. OpenCV global thread settings
are left unchanged. Benchmark alongside other processes; no hard real-time promise.
adapters.yuy2_gray(buffer,width,height,stride) exposes YUY2 luminance without conversion.

## QNX handoff

Existing pi/pi_stream.py uses camera_example4_record, unit 5, 640x480 YUY2,
tails UCV files and JPEG-streams to laptop/receiver.py. The receiver adds only
laptop arrival times; it has neither stereo identities nor capture timestamps.

Do NOT start two copies: the streamer kills all matching recorder processes and
deletes shared UCV files on startup; file discovery cannot safely identify two cameras.
Those existing files remain unchanged. Two identified timestamped feeds are needed.
The generic processing API is ready; a verified QNX capture adapter remains outstanding.

Run the standard-library-only probe directly on the target:
```sh
python3 probe_qnx.py > qnx-capabilities.json
```
It checks OS, Python imports, binaries, headers, devices and QNX USB/process inventory
without starting/stopping capture. QNX Camera API documentation describes libcamapi
callbacks and monotonic microsecond frame timestamps. Verify the installed ABI and
driver semantics before implementing a bridge; convert verified microseconds to seconds.

QNX has an OpenCV port, but Windows wheels cannot be copied onto QNX. Confirm target
cv2/NumPy and calib3d/imgproc support first. Published port builds use a Linux host.
Laptop processing is a practical test path, not proof of on-Pi functionality.

Calibration transfers only if the SAME physical pair, fixed mount, resolution,
crop, orientation and compatible camera mode are preserved. Verify epipolar
alignment again after switching OS. Changing mounting requires recalibration.

References:
- [QNX OpenCV port](https://github.com/qnx-ports/build-files/blob/main/ports/opencv/README.md)
- [QNX camera callbacks](https://get.qnx.com/developers/docs/7.0.0/com.qnx.doc.camera/topic/camera_start_viewfinder.html)
- [QNX buffer timestamps](https://get.qnx.com/developers/docs/7.0.0/com.qnx.doc.camera/topic/structcamera__buffer__t.html)
- [OpenCV calibration](https://docs.opencv.org/4.13.0/d9/d0c/group__calib3d.html)

Older Camera API docs are a lead, not proof of this QNX 8 image's interface.

## Independent tests and replay

```powershell
python -m unittest discover -s stereo_depth/tests -v
python -m stereo_depth demo --frames 100
python -m stereo_depth demo --frames 1 --output stereo_depth/artifacts/demo
python -m stereo_depth replay --calibration pair.npz --manifest pairs/manifest.json --show
```

Calibration/replay manifest: {"left_id":"left-c270","right_id":"right-c270",
"pairs":[{"left":"left_000.png","right":"right_000.png"}]}.
Paths are relative to the JSON. Add timestamps_s:[lt,rt] and clock_domain to each
pair when available. Without timestamps calibration needs explicit --static-pairs.

The optional generic live runner takes --source your_feed:paired_frames, a factory
yielding pairs of Frame objects. Depth computation and visualization stay independent.
