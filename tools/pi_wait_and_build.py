"""Waits for the Pi to appear on the hotspot, then starts the on-Pi AI build the moment it can. Usage: python tools/pi_wait_and_build.py [--clean]   (default RESUMES an interrupted build; --clean starts from nothing)

It scans 192.168.137.x for SSH every few seconds; when the Pi answers it copies qnx/ai to ~/ai/fw, stops the tracker, streamer and
camera on the Pi (so every core goes to the compile), and starts `sh build.sh` detached (it keeps running if this connection drops).
It then exits; follow progress with tools/pi_build_watch.py.
"""
import concurrent.futures as cf
import os
import socket
import subprocess
import sys
import time

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
KEY = os.path.expanduser("~/.ssh/turret_ed25519")
OPTS = ["-i", KEY, "-o", "MACs=hmac-sha2-256", "-o", "BatchMode=yes", "-o", "HostKeyAlias=qnxpi", "-o", "StrictHostKeyChecking=yes",
        "-o", "ConnectTimeout=25"]
FILES = ["CMakeLists.txt", "facewatch.cc", "build.sh", "mkpreload.py", "resume.sh"]
SCRIPT = "resume.sh" if "--fast" in sys.argv else "build.sh"


def probe(ip):
    try:
        with socket.create_connection((ip, 22), timeout=1.0):
            return ip
    except OSError:
        return None


def find_pi():
    with cf.ThreadPoolExecutor(64) as ex:
        hits = [h for h in ex.map(probe, [f"192.168.137.{i}" for i in range(2, 255)]) if h]
    return hits[0] if hits else None


def ssh(ip, cmd, timeout=120):
    return subprocess.run(["ssh"] + OPTS + [f"qnxuser@{ip}", cmd], capture_output=True, text=True, timeout=timeout)


def main():
    t0, last = time.time(), 0
    ip = None
    while ip is None:
        ip = find_pi()
        if ip is None:
            if time.time() - last > 60:
                print(f"{(time.time() - t0) / 60:.0f} min: the Pi is not on the hotspot yet", flush=True)
                last = time.time()
            time.sleep(4)
    print(f"Pi reachable at {ip} after {(time.time() - t0) / 60:.1f} min", flush=True)
    r = ssh(ip, "uname -a | cut -c1-60; pidin info | head -1")
    print(r.stdout.strip(), flush=True)
    ssh(ip, "mkdir -p ~/ai/fw")
    for f in FILES:
        subprocess.run(["scp"] + OPTS + [os.path.join(ROOT, "qnx", "ai", f), f"qnxuser@{ip}:ai/fw/{f}"], capture_output=True, timeout=60)
    print("files copied", flush=True)
    ssh(ip, "slay -Q -f pi_stream.py; slay -Q turretd; slay -Q -f camera_example4_record; slay -Q facewatch; slay -Q -f 'python3 -c'")
    if "--clean" in sys.argv:
        ssh(ip, "rm -rf ~/ai/fwbuild")
    else:
        # RESUME: a hard power-off can leave a half-written object file that make would trust. Delete the newest few so they are rebuilt.
        r = ssh(ip, "cd ~/ai/fwbuild 2>/dev/null && for f in $(ls -t $(find . -name '*.o' 2>/dev/null) 2>/dev/null | head -4); do rm -f $f; echo removed $f; done; echo resume-ready")
        print(r.stdout.strip(), flush=True)
    ssh(ip, "cd ~/ai/fw && (nohup sh " + SCRIPT + " >> ~/ai/build_fw.log 2>&1 &); sleep 2; echo started")
    time.sleep(5)
    r = ssh(ip, "pidin arg 2>/dev/null | grep -E 'cmake|make' | grep -v grep | wc -l; ls ~/ai/models; pidin info | head -1")
    print("BUILD STARTED on the Pi. Running build processes / model / memory:\n" + r.stdout.strip(), flush=True)
    print(ip)


if __name__ == "__main__":
    main()
