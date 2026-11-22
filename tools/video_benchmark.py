"""Benchmarks the video path: starts the dashboard server in each configuration, lets it settle, and reads what arrived over the Wi-Fi
(rate, KB per picture, gaps, delay from the Pi's clock) plus what the Pi measured about itself (encode ms, CPU %). Configurations run
in alternating order, several times, because the Wi-Fi changes from minute to minute: one run proves nothing.

    python tools/video_benchmark.py                      the default comparison (full size quality 90 vs 480x360 quality 65), 2 rounds
    python tools/video_benchmark.py --rounds 3 --seconds 60
    python tools/video_benchmark.py --config "full:90" --config "480:50"       custom configurations (display:quality)

The dashboard must not already be running (it would hold port 8000 and the camera). Nothing is changed on the Pi or in the project.
"""
import argparse
import json
import os
import statistics
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


def get_state():
    return json.load(urllib.request.urlopen("http://127.0.0.1:8000/state", timeout=5))


def run_config(display, quality, settle, seconds):
    env = dict(os.environ, TURRET_DISPLAY=display, TURRET_QUALITY=str(quality))
    proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "laptop", "app.py")], env=env, cwd=ROOT,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        t0 = time.time()
        while time.time() - t0 < 90:                                  # wait for the first pictures from the Pi
            try:
                if get_state().get("connected"):
                    break
            except Exception:
                pass
            time.sleep(1)
        else:
            return None
        time.sleep(settle)
        rows = []
        end = time.time() + seconds
        while time.time() < end:
            try:
                rows.append(get_state())
            except Exception:
                pass
            time.sleep(2)
        wire = [r["video_wire"] for r in rows if r.get("video_wire")]
        pi = [r["pi_video"] for r in rows if r.get("pi_video")]
        if not wire:
            return None
        last = wire[-1]                                               # the last 30 s window
        return {"fps": last["fps"], "kb": last["kb"], "gap_p50": last["gap_p50"], "gap_p95": last["gap_p95"], "gap_p99": last["gap_p99"],
                "gap_max": last["gap_max"], "over_200": last["over_200"], "n": last["n"],
                "delay_p50": last.get("delay_p50"), "delay_p95": last.get("delay_p95"),
                "pi_encode_ms": statistics.mean(p["encode_ms"] for p in pi) if pi else None,
                "pi_cpu": statistics.mean(p["cpu_pct"] for p in pi) if pi else None,
                "udp_rtt": rows[-1]["qnx"]["rtt"].get("udp", {}).get("p50") if rows[-1].get("qnx") else None}
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
        time.sleep(4)                                                 # let the Pi's streamer see the connection close


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", action="append", help="display:quality, e.g. full:90 or 480:65 (repeat for several)")
    p.add_argument("--rounds", type=int, default=2)
    p.add_argument("--seconds", type=int, default=45, help="how long to collect after settling (the stats window is the last 30 s)")
    p.add_argument("--settle", type=int, default=10)
    a = p.parse_args()
    configs = [tuple(c.split(":")) for c in (a.config or ["full:90", "480:65"])]
    results = {c: [] for c in configs}
    for rnd in range(a.rounds):
        order = configs if rnd % 2 == 0 else list(reversed(configs))   # alternate the order so drift in the Wi-Fi cancels out
        for display, quality in order:
            print(f"round {rnd + 1}/{a.rounds}: display={display} quality={quality} ...", flush=True)
            r = run_config(display, int(quality), a.settle, a.seconds)
            print("   ", r if r else "no result (the Pi did not connect)", flush=True)
            if r:
                results[(display, quality)].append(r)
    print("\n=== summary (mean over rounds) ===")
    cols = [("fps", "fps"), ("kb", "KB/pic"), ("gap_p50", "gap p50"), ("gap_p95", "gap p95"), ("gap_p99", "gap p99"), ("gap_max", "worst"),
            ("over_200", ">200ms"), ("delay_p50", "delay p50"), ("delay_p95", "delay p95"), ("pi_encode_ms", "enc ms"), ("pi_cpu", "Pi CPU%")]
    print(f"{'config':14s}" + "".join(f"{c[1]:>10s}" for c in cols))
    for (display, quality), runs in results.items():
        cells = []
        for key, _ in cols:
            vals = [r[key] for r in runs if r.get(key) is not None]
            cells.append(f"{statistics.mean(vals):10.1f}" if vals else f"{'-':>10s}")
        print(f"{display + ' q' + quality + ' (n=' + str(len(runs)) + ')':14s}" + "".join(cells))


if __name__ == "__main__":
    main()
