# QNX side (runs on the Raspberry Pi under QNX 8.0)

Plan: make QNX the turret's brain and the laptop its display. The depth network and anything else heavy stay on the laptop.

| Step | What | Status |
|---|---|---|
| 1 | Color tracker in C (`tracker.c`), same answers as `laptop/tracker.py` | written, **not yet compiled or tested** (no C compiler yet) |
| 2 | Mission state machine and fire interlock (ARM, shot limit, confidence threshold) in C | not started |
| 3 | Watchdog: heartbeat from the laptop; on loss, force the safe state (disarmed, servos held) | not started |
| 4 | Fixed-rate real-time control loop, with its own timing statistics | not started |
| 5 | QNX health panel on the Diagnostics page (capture/encode rate, CPU, memory, jitter) | not started |

Build and test on the laptop: `python tests/test_qnx_tracker.py` (builds `tracker.c` with gcc, clang or `zig cc` and compares it with the Python tracker).
Build for QNX: install the QNX SDP 8.0, run `qnxsdp-env.bat`, then in `qnx/`: `make CC="qcc -Vgcc_ntoaarch64le"`.
