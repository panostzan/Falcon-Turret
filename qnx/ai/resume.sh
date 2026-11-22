#!/bin/sh
# FAST PATH: finish the ORIGINAL configure in ~/ai/build. Its dependencies and compiler checks are already on the Pi; only the final
# "install export" step failed. Everything runs at priority 5, below the Pi's network and SSH (10), so the compile can never starve them.
# "install export" step failed. So this downloads nothing and skips the slow checks. Then it compiles just the TensorFlow Lite library.
cd "$HOME/ai/build" || { echo "no ~/ai/build"; exit 1; }
echo "=== what is here ==="; ls | head -40
FLAGS="-D_QNX_SOURCE -DFARMHASH_LITTLE_ENDIAN -D__LITTLE_ENDIAN__ -O1"
echo "=== reconfigure (install export off) ==="
on -p 5 cmake -DTFLITE_ENABLE_INSTALL=OFF -DABSL_ENABLE_INSTALL=OFF -DCMAKE_C_FLAGS="$FLAGS" -DCMAKE_CXX_FLAGS="$FLAGS" . || { echo "RECONFIGURE FAILED"; exit 2; }
echo "=== building the library ==="
on -p 5 make -j${JOBS:-3} tensorflow-lite || { echo "LIBRARY BUILD FAILED"; exit 3; }
echo "=== BUILD DONE ==="
