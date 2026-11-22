# QNX side (runs on the Raspberry Pi under QNX 8.0)

Plan: make QNX the turret's brain and the laptop its display. The depth network and anything else heavy stay on the laptop.

| Step | What | Status |
|---|---|---|
| 1 | Color tracker in C (`tracker.c`), same answers as `laptop/tracker.py` | **built with the Pi's own gcc on QNX 8.0 and verified**: 20 frames, identical to the Python tracker (0 disagreements, 0.0 px), 9 ms per frame on the Pi |
| 2 | Mission state machine and fire interlock (ARM, shot limit, confidence threshold) in C | not started |
| 3 | Watchdog: heartbeat from the laptop; on loss, force the safe state (disarmed) | **done, in `turretd`, verified on the Pi** (disarms 1.5 s after the heartbeat stops and stays disarmed) |
| 4 | `turretd`: one QNX program = tracker + interlock + watchdog, fed raw camera pictures, controlled over UDP | **built and tested on the Pi with synthetic camera pictures** (10 checks pass; about 8 ms per picture); not yet fed from the live camera |
| 5 | QNX health panel on the Diagnostics page (capture/encode rate, CPU, memory, jitter) | not started |

Build and run on the Pi (it has gcc): copy `tracker.c`, `tracker.h`, `tracker_demo.c` to `~/turret_c`, then `gcc -O2 -std=c99 -o tracker_demo tracker_demo.c tracker.c -lm`.
Build and test on the laptop: `python tests/test_qnx_tracker.py` (builds `tracker.c` with gcc, clang or `zig cc` and compares it with the Python tracker).
Build for QNX: install the QNX SDP 8.0, run `qnxsdp-env.bat`, then in `qnx/`: `make CC="qcc -Vgcc_ntoaarch64le"`.

Build turretd on the Pi: `gcc -O2 -std=gnu99 -o turretd turretd.c tracker.c engage.c -lm -lsocket` (QNX needs -lsocket).
Test on the Pi: `python3 test_turretd.py frames.yuy2 640 480` (frames.yuy2 = raw YUY2 pictures).
