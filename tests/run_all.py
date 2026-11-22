"""Runs every test (Python suites plus the dashboard smoke test if Node is installed)."""
import glob
import os
import shutil
import subprocess
import sys

here = os.path.dirname(os.path.abspath(__file__))
failed = []
for f in sorted(glob.glob(os.path.join(here, "test_*.py"))):
    r = subprocess.run([sys.executable, f], capture_output=True, text=True, env=dict(os.environ, TURRET_TILT_CALIBRATION="none.json"))
    last = (r.stdout.strip().splitlines() or [r.stderr.strip()[-200:]])[-1]
    print(f"{'ok  ' if r.returncode == 0 else 'FAIL'} {os.path.basename(f):24s} {last}")
    if r.returncode:
        failed.append(f)
if shutil.which("node"):
    r = subprocess.run(["node", os.path.join(here, "dashboard_smoke.js")], capture_output=True, text=True)
    print(f"{'ok  ' if r.returncode == 0 else 'FAIL'} {'dashboard_smoke.js':24s} {(r.stdout or r.stderr).strip()}")
    if r.returncode:
        failed.append("dashboard_smoke.js")
sys.exit(1 if failed else 0)
