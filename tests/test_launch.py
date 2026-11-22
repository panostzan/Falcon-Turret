"""The launcher's helper functions. Run: python tests/test_launch.py"""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import launch  # noqa: E402

launch.PORT = 18766                         # not the real dashboard's port: this test must work while the dashboard is running
launch.URL = f"http://127.0.0.1:{launch.PORT}"

fails = 0


def check(name, ok, detail=""):
    global fails
    print(("PASS " if ok else "FAIL ") + name + (" - " + detail if detail else ""))
    fails += 0 if ok else 1


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"connected": True, "frame_age": 0.2} if self.path == "/state" else {}).encode()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


check("no server on the port: not up", launch.server_up() is False)
srv = HTTPServer(("127.0.0.1", launch.PORT), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
check("a dashboard-like server: up", launch.server_up() is True)
check("camera_live reads the state", launch.camera_live() is True)
srv.shutdown()
srv.server_close()
b = launch.find_browser()
check("browser lookup returns a real file or None", b is None or os.path.exists(b), str(b))
check("the launcher script exists next to Turret.bat", os.path.exists(os.path.join(os.path.dirname(launch.__file__), "Turret.bat")))
print("\nALL PASSED" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
