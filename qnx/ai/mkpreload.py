"""Turns the compiler-check results saved by an earlier CMake run (~/ai/build/CMakeCache.txt) into a preload file, so the next configure
does not repeat the same slow checks on the Pi. Usage: python3 mkpreload.py CMakeCache.txt preload.cmake"""
import re
import sys

KEEP = re.compile(r"^(COMPILER_SUPPORT_|FARMHASH_|EIGEN_|HAVE_|HAS_|[A-Z0-9_]*_HAS_|[A-Z0-9_]*_SUPPORTED$|[A-Z0-9_]*_COMPILES$|STD_|CXX_|C_)")
n = 0
with open(sys.argv[1], encoding="utf-8", errors="replace") as src, open(sys.argv[2], "w", encoding="utf-8") as out:
    for line in src:
        m = re.match(r"^([A-Za-z0-9_+\-]+):INTERNAL=(.*)$", line.rstrip())
        if m and KEEP.match(m.group(1)):
            out.write('set(%s "%s" CACHE INTERNAL "")\n' % (m.group(1), m.group(2).replace('"', '\\"')))
            n += 1
print("preloaded %d saved check results" % n)
