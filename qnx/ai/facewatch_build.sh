#!/bin/sh
# Final step: compile facewatch against the TensorFlow Lite library that resume.sh built in ~/ai/build. Takes the exact include paths and
# flags TensorFlow Lite itself was compiled with (CMake's flags.make for the library), so nothing has to be guessed.
# Output: ~/ai/fwbuild/facewatch
set -e
B="$HOME/ai/build"
FM="$B/CMakeFiles/tensorflow-lite.dir/flags.make"
OUT="$HOME/ai/fwbuild/facewatch"
mkdir -p "$HOME/ai/fwbuild"
[ -f "$B/libtensorflow-lite.so" ] || { echo "libtensorflow-lite.so is not built yet"; exit 1; }
[ -f "$FM" ] || { echo "no flags.make"; exit 1; }
val() { grep "^$1 = " "$FM" | sed "s/^$1 = //"; }
INC=$(val CXX_INCLUDES); DEF=$(val CXX_DEFINES); FLG=$(val CXX_FLAGS)
# every folder holding a shared library the main one depends on
LD=""
for f in $(find "$B" -name '*.so*' 2>/dev/null); do
  d=$(dirname "$f")
  case " $LD " in *" -L$d "*) ;; *) LD="$LD -L$d -Wl,-rpath,$d -Wl,-rpath-link,$d" ;; esac
done
# QNX's cpuinfo only compiles its QNX files when CMake's QNX switch is on (set by QNX's cross-compile setup, which a native build does not use).
# So the library was built without them; compile them here and link them into facewatch (the library finds them there).
CF="$B/_deps/cpuinfo-build/CMakeFiles/cpuinfo.dir/flags.make"
cval() { grep "^$1 = " "$CF" | sed "s/^$1 = //"; }
CINC=$(cval C_INCLUDES); CDEF=$(cval C_DEFINES); CFLG=$(cval C_FLAGS)
QOBJ=""
for f in "$B/cpuinfo/src/qnx/api.c" "$B/cpuinfo/src/arm/uarch.c"; do     # the two files cpuinfo adds in QNX mode
  o="$HOME/ai/fwbuild/cpuinfo_$(basename "$(dirname "$f")")_$(basename "$f" .c).o"
  on -p 5 gcc -c $CDEF $CINC $CFLG "$f" -o "$o"
  QOBJ="$QOBJ $o"
done
echo "compiled the QNX cpuinfo files:$QOBJ"
# Put them INSIDE libcpuinfo.so (its other symbols are private to it): relink just that one small library, once.
CB="$B/_deps/cpuinfo-build"
if [ ! -f "$CB/.qnx_relinked" ]; then
  [ -f "$CB/libcpuinfo.so" ] || cp "$CB/libcpuinfo.so.orig" "$CB/libcpuinfo.so"        # a failed link deletes its output: restore it
  [ -f "$CB/libcpuinfo.so.orig" ] || cp "$CB/libcpuinfo.so" "$CB/libcpuinfo.so.orig"
  ( cd "$CB" && eval "$(cat CMakeFiles/cpuinfo.dir/link.txt) $QOBJ" )
  touch "$CB/.qnx_relinked"
  echo "relinked libcpuinfo.so with the QNX files"
fi
echo "=== compiling facewatch ==="
echo "flags: $FLG"
on -p 5 g++ $DEF $INC $FLG "$HOME/ai/fw/facewatch.cc" -o "$OUT" -L"$B" -ltensorflow-lite -lcpuinfo $LD -lsocket
echo "=== FACEWATCH BUILT ==="
ls -l "$OUT"
