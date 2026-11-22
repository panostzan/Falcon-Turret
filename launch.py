"""One-click launcher: starts the dashboard server, waits for the Pi's camera, runs the system check, and opens the dashboard in its
own window. Double-click Turret.bat (it runs this). Closing this window, or pressing Ctrl+C, stops the server.

    python launch.py                 start everything
    python launch.py --no-window     do everything except opening the browser window
    python launch.py --wiggle        also wiggle the servos in the pre-flight check (keep hands clear)
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
PORT = 8000
URL = f"http://127.0.0.1:{PORT}"
SERVER_LOG = os.path.join(ROOT, "captures", "server.log")


def get_json(path, post=False, timeout=5):
    req = urllib.request.Request(URL + path, method="POST" if post else "GET")
    return json.load(urllib.request.urlopen(req, timeout=timeout))


def server_up():
    """True when OUR dashboard server answers on the port (something else holding the port does not count)."""
    try:
        return "connected" in get_json("/state", timeout=2)
    except Exception:
        return False


def find_browser():
    """Edge or Chrome, whichever is installed (both can show a page as a plain app window). None when neither is found."""
    roots = [os.environ.get(k) for k in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA")]
    for sub in (r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe"):
        for root in roots:
            if root and os.path.exists(os.path.join(root, sub)):
                return os.path.join(root, sub)
    return None


def open_window():
    exe = find_browser()
    if exe:
        subprocess.Popen([exe, f"--app={URL}", "--window-size=1366,800"])
        return True
    import webbrowser
    webbrowser.open(URL)
    return False


def say(text=""):
    print(text, flush=True)


def wait_for(cond, seconds, label):
    """Wait up to `seconds` for cond(); prints a dot per second. Returns True when it came true."""
    end = time.time() + seconds
    sys.stdout.write(f"   {label}")
    sys.stdout.flush()
    while time.time() < end:
        if cond():
            say(" ok")
            return True
        sys.stdout.write(".")
        sys.stdout.flush()
        time.sleep(1)
    say(" not yet")
    return False


def camera_live():
    try:
        s = get_json("/state")
        return bool(s.get("connected")) and s.get("frame_age", 99) < 2.5
    except Exception:
        return False


def main():
    no_window, wiggle = "--no-window" in sys.argv, "--wiggle" in sys.argv
    say("Turret launcher")
    say("---------------")
    proc = None
    if server_up():
        say("1. The dashboard server is already running: using it.")
    else:
        say("1. Starting the dashboard server...")
        os.makedirs(os.path.dirname(SERVER_LOG), exist_ok=True)
        log = open(SERVER_LOG, "ab")
        proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "laptop", "app.py")], cwd=ROOT, stdout=log, stderr=log)
        if not wait_for(lambda: server_up() or proc.poll() is not None, 45, "waiting for the server") or proc.poll() is not None:
            say(f"   The server did not start. Is something else using port {PORT}? Details: {SERVER_LOG}")
            input("Press Enter to close.") if sys.stdin.isatty() else None
            return 1
    say("2. Looking for the Pi and its camera (up to 60 s; the Pi needs a minute after power-up)...")
    live = wait_for(camera_live, 60, "waiting for pictures")
    if not live:
        say("   No pictures yet. The window will still open; the Check page says what is wrong.")
    say("3. Pre-flight check" + (" (with servo wiggle)" if wiggle else "") + "...")
    try:
        r = get_json(f"/syscheck?wiggle={1 if wiggle else 0}", post=True, timeout=60)
        icon = {"pass": "OK  ", "warn": "WARN", "fail": "FAIL", "skip": "skip"}
        for row in r["rows"]:
            say(f"   [{icon[row['state']]}] {row['name']}: {row['detail']}")
            if row["state"] in ("warn", "fail") and row["fix"]:
                say(f"          -> {row['fix']}")
        say(f"   Result: {r['summary']['verdict'].upper()}")
    except Exception as e:
        say(f"   Could not run the check: {e}")
    if no_window:
        say("4. Window not opened (--no-window).")
    else:
        say("4. Opening the dashboard window...")
        open_window()
    say()
    say(f"Dashboard: {URL}     Close this window or press Ctrl+C to stop everything.")
    try:
        while proc is None or proc.poll() is None:
            time.sleep(1)
        say(f"The server stopped (code {proc.returncode}). Details: {SERVER_LOG}")
    except KeyboardInterrupt:
        pass
    finally:
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
            say("Server stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
