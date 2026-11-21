"""Read-only, standard-library-only target probe. Run directly on QNX Python.

Does not import stereo_depth, open cameras, start recorders, or change services.
"""
import ctypes.util
import glob
import json
import os
import platform
import shutil
import subprocess
import sys


def main():
    report = {"platform": platform.platform(), "python": sys.version,
              "is_qnx": platform.system() == "QNX", "commands": {}, "libraries": {}, "modules": {}}
    for name in ("qcc", "QCC", "camera_example4_record", "vcap-mgr", "sensor", "usb", "pidin"):
        report["commands"][name] = shutil.which(name)
    for name in ("camapi", "capture", "turbojpeg"):
        report["libraries"][name] = ctypes.util.find_library(name)
    for name in ("numpy", "cv2"):
        code = "import " + name + "; print(" + name + ".__version__)"
        if name == "cv2":
            code += "; print({k:hasattr(cv2,k) for k in ['StereoBM_create','StereoSGBM_create','stereoRectify','initUndistortRectifyMap','remap']})"
        try:
            p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=20)
            report["modules"][name] = {"ok": p.returncode == 0, "output": (p.stdout+p.stderr)[-3000:]}
        except Exception as exc:
            report["modules"][name] = {"ok": False, "error": str(exc)}
    report["camera_headers"] = [p for pattern in ("/usr/include/camera/*.h", "/usr/local/include/camera/*.h") for p in glob.glob(pattern)]
    report["camera_libraries"] = [p for pattern in ("/usr/lib/libcam*", "/usr/lib/libcapture*", "/lib/dll/*uvc*", "/usr/lib/dll/*uvc*") for p in glob.glob(pattern)]
    report["device_names"] = os.listdir("/dev") if os.path.isdir("/dev") else []
    if report["is_qnx"]:
        for name, args in (("usb", ["usb", "-v"]), ("processes", ["pidin", "ar"])):
            try:
                p = subprocess.run(args, capture_output=True, text=True, timeout=10)
                report[name] = {"exit_code": p.returncode, "output": (p.stdout+p.stderr)[-20000:]}
            except Exception as exc:
                report[name] = {"error": str(exc)}
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
