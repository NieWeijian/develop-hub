#!/bin/sh
set -eu
cd "$(dirname "$0")"
# Build on the ARM64 3588 bench. This builds a host test helper, not module firmware.
g++ -O2 -std=c++17 -pthread -Ihost-tools-source/common host_capture.cpp host-tools-source/common/v4l2_capture.cpp -o host-capture
