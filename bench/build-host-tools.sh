#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
: "${CXX:?Set CXX to an AArch64 Linux C++ compiler}"
mkdir -p test-bins/ctl-trace-1
"$CXX" -std=c++17 -O2 -static -pthread -Ihost-tools-source/common host-tools-source/uvc_host_ctl.cpp host-tools-source/common/v4l2_capture.cpp -o test-bins/ctl-trace-1/uvc_host_ctl
sha256sum test-bins/ctl-trace-1/uvc_host_ctl
