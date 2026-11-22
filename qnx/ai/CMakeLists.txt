# The on-device AI program (facewatch): our code plus QNX's TensorFlow Lite port, built together on the Pi.
# Build with qnx/ai/build.sh. TensorFlow Lite is added as a subdirectory (the way TensorFlow's own docs recommend), which also
# avoids its "install" step: we only need the library, not an installed copy.
cmake_minimum_required(VERSION 3.16)
project(facewatch CXX)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

set(TFLITE_SRC "$ENV{HOME}/ai/tensorflow/tensorflow/lite" CACHE PATH "QNX's TensorFlow fork: tensorflow/lite")
add_subdirectory(${TFLITE_SRC} ${CMAKE_CURRENT_BINARY_DIR}/tflite EXCLUDE_FROM_ALL)

add_executable(facewatch facewatch.cc)
target_link_libraries(facewatch tensorflow-lite)
