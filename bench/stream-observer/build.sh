#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
common=tool-packages/firmware-tools-20260929-cc3f234-hub1/source/tools/common
g++ -O2 -std=c++17 -pthread -I"$common" stream-observer/uvc_host_stream.cpp "$common/v4l2_capture.cpp" -o uvc_host_stream-metrics
