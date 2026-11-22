# Uncalibrated live disparity

User-measured approximate lens-centre spacing: **141 mm (0.141 m)**.
This is a reference measurement, not a completed calibration. It is intentionally
not used to manufacture metric depth without verified intrinsics/extrinsics.

Run from the repository root:

```powershell
python -m stereo_depth.experimental --left 1 --right 2
```

Top: raw camera views. Bottom: rectified left image and a signed disparity map.
Black means invalid/no reliable match. Colors represent pixel disparity, **not
metres**. Projective uncalibrated rectification does not guarantee globally accurate
near/far ordering. This experimental API intentionally exposes no `depth_m` field
and is separate from the calibrated processor.

The program estimates alignment using shared natural SIFT features and RANSAC,
validates on held-out matches, and refuses weak/unstable geometry. Dense matching
uses SGBM, a reverse matcher, overlap masks, and low-texture rejection. Estimated
alignment is held fixed until reset; press **R** whenever either camera is moved
relative to the other. **Q/Escape** closes and releases cameras.

For initial alignment, show both cameras the same stationary scene containing
distinct details at several distances. Books, packaging and furniture work better
than blank walls, repeating ceiling stripes or a face very close to the cameras.
Hold the rig still. Initial alignment retries automatically every two seconds.
It does not compensate for lens distortion or establish exposure synchronization.

Validation: 19 total local tests passed, including nonplanar synthetic uncalibrated
rectification/disparity and rejection of textureless input. The first 50-pair real
camera trial received images but correctly refused alignment with only 14 shared
features. No successful physical-scene depth or accuracy claim follows from that run.

Reference: OpenCV's
[uncalibrated rectification](https://docs.opencv.org/4.13.0/d9/d0c/group__calib3d.html)
uses projective homographies instead of calibrated camera rotations.
