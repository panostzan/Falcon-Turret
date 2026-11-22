"""The Pi's `top` reader, checked against real output captured on the Pi."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "pi"))
import sysmon

fails = 0


def check(name, ok, detail=""):
    global fails
    print(("PASS " if ok else "FAIL ") + name + (" - " + detail if detail else ""))
    fails += 0 if ok else 1


text = open(os.path.join(HERE, "fixtures", "qnx_top.txt"), "rb").read().decode(errors="replace")
p = sysmon.parse_top(text)
check("header: 4 CPUs, process and thread counts", p["cpus"] == 4 and p["processes"] and p["threads"], f"{p['cpus']} cpus {p['processes']} procs")
check("CPU and memory totals", p["user"] is not None and p["kernel"] is not None and p["mem_total"] == 8128, f"{p['user']}% user {p['kernel']}% kernel")
check("thread rows parsed", len(p["rows"]) >= 5 and all(r["name"] and r["prio"] > 0 for r in p["rows"]), f"{len(p['rows'])} rows")
s = sysmon.summarize(p)
check("summary: tracker found with its priority", s["tracker_prio"] == 10, str(s["tracker_prio"]))
check("summary: programs added up, busiest first", [t["cpu"] for t in s["top"]] == sorted((t["cpu"] for t in s["top"]), reverse=True))
check("summary: drm threads of one program merged", sum(1 for t in s["top"] if t["name"] == "drm-rpi5") <= 1)
synth = {"rows": [{"pid": 100 + i, "tid": 1, "prio": 10, "state": "Run", "cpu": 12.0, "name": "python3"} for i in range(8)] + [{"pid": 7, "tid": 1, "prio": 30, "state": "SigW", "cpu": 3.0, "name": "turretd"}], "mem_total": 8000, "mem_avail": 7000, "user": 90.0, "kernel": 5.0, "cpus": 4, "processes": 9, "threads": 9}
ss = sysmon.summarize(synth)
check("summary: 8 identical programs become one entry, the tracker stays visible", any(t["name"] == "python3" and t["n"] == 8 and t["cpu"] == 96.0 for t in ss["top"]) and any(t["name"] == "turretd" and t["prio"] == 30 for t in ss["top"]), str(ss["top"]))
check("odd output never raises", sysmon.summarize(sysmon.parse_top("garbage\n\x1b[H")) ["top"] == [])

pushed = []
m = sysmon.Monitor(pushed.append)
check("handle: only its own commands", m.handle("stress maybe") is False and m.handle("preset teal") is False and m.handle("aim 1 3") is False)
sysmon.set_tracker_priority = lambda prio: pushed.append(("prio", prio)) or True
check("handle: rt off then on", m.handle("rt off") and m.handle("rt on") and pushed == [("prio", 10), ("prio", 30)], str(pushed))
print("\nALL PASSED" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
