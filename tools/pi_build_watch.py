"""Follows the on-Pi AI build (started by tools/pi_wait_and_build.py): prints the progress when it changes, ends on done or failure.
Usage: python tools/pi_build_watch.py [pi_ip]"""
import os
import re
import subprocess
import sys
import time

IP = sys.argv[1] if len(sys.argv) > 1 else "192.168.137.249"
KEY = os.path.expanduser("~/.ssh/turret_ed25519")
OPTS = ["-i", KEY, "-o", "MACs=hmac-sha2-256", "-o", "BatchMode=yes", "-o", "HostKeyAlias=qnxpi", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=30"]


def ssh(cmd):
    try:
        return subprocess.run(["ssh"] + OPTS + [f"qnxuser@{IP}", cmd], capture_output=True, text=True, timeout=90).stdout
    except Exception:
        return ""


t0, last, lost = time.time(), "", 0
while time.time() - t0 < 7200:
    out = ssh("tail -c 4000 ~/ai/build_fw.log; echo '@@'; pidin info | head -1")
    if not out:
        lost += 1
        if lost in (1, 3, 6):
            print(f"{(time.time() - t0) / 60:.0f} min: no answer from the Pi ({lost} in a row)", flush=True)
        time.sleep(30)
        continue
    lost = 0
    log, _, mem = out.partition("@@")
    if "BUILD DONE" in log:
        print(f"BUILD DONE after {(time.time() - t0) / 60:.0f} min of watching", flush=True)
        break
    if re.search(r"CMake Error|\*\*\* \[|make: \*\*\*|error:", log) and not re.search(r"\[\s*\d+%\]", log[-300:]):
        print("BUILD FAILED:\n" + log[-1800:], flush=True)
        break
    pct = re.findall(r"\[\s*(\d+)%\]", log)
    free = re.search(r"FreeMem:(\d+)MB", mem)
    state = (pct[-1] + "%") if pct else "configuring"
    if state != last:
        print(f"{(time.time() - t0) / 60:.0f} min: {state}  (Pi free memory {free.group(1) + ' MB' if free else '?'})", flush=True)
        last = state
    time.sleep(45)
print(ssh("tail -c 600 ~/ai/build_fw.log"))
