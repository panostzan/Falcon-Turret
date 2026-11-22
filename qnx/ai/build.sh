#!/bin/sh
# Build facewatch (and the QNX TensorFlow Lite port it uses) natively on the Pi. Run from ~/ai/fw:  sh build.sh
# Sources were fetched into ~/ai (qnx-ports/tensorflow and build-files). Two parallel jobs keep memory modest on an 8 GB Pi.
set -e
cd "$HOME/ai"
mkdir -p fwbuild && cd fwbuild
FLAGS="-D_QNX_SOURCE -DFARMHASH_LITTLE_ENDIAN -D__LITTLE_ENDIAN__"
cmake ../fw \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_C_FLAGS="$FLAGS" -DCMAKE_CXX_FLAGS="$FLAGS" \
  -DFETCHCONTENT_BASE_DIR="$HOME/ai/build/_deps" \
  -DBUILD_SHARED_LIBS=ON -DTFLITE_ENABLE_INSTALL=OFF -DTFLITE_ENABLE_XNNPACK=OFF -DTFLITE_ENABLE_GPU=OFF -DTFLITE_KERNEL_TEST=OFF \
  -DOVERRIDABLE_FETCH_CONTENT_cpuinfo_GIT_REPOSITORY=https://github.com/qnx-ports/cpuinfo -DOVERRIDABLE_FETCH_CONTENT_cpuinfo_GIT_TAG=qnx \
  -DOVERRIDABLE_FETCH_CONTENT_ruy_GIT_REPOSITORY=https://github.com/qnx-ports/ruy -DOVERRIDABLE_FETCH_CONTENT_ruy_GIT_TAG=master \
  -DOVERRIDABLE_FETCH_CONTENT_abseil-cpp_GIT_REPOSITORY=https://github.com/qnx-ports/abseil-cpp.git -DOVERRIDABLE_FETCH_CONTENT_abseil-cpp_GIT_TAG=qnx_20230802.1 \
  -DOVERRIDABLE_FETCH_CONTENT_farmhash_GIT_REPOSITORY=https://github.com/qnx-ports/farmhash.git -DOVERRIDABLE_FETCH_CONTENT_farmhash_GIT_TAG=qnx \
  -DOVERRIDABLE_FETCH_CONTENT_eigen_GIT_REPOSITORY=https://github.com/qnx-ports/eigen.git -DOVERRIDABLE_FETCH_CONTENT_eigen_GIT_TAG=qnx_v3.4.1
echo "=== configured; building with 2 jobs ==="
make -j2 facewatch
echo "=== BUILD DONE ==="
