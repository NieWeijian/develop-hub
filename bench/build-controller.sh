#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
: "${CROSS_CXX:=/home/nwj/RK3572_struct_light/prebuilts/gcc/linux-x86/aarch64/arm-gnu-toolchain-14.3.rel1-x86_64-aarch64-none-linux-gnu/bin/aarch64-none-linux-gnu-g++}"
"$CROSS_CXX" -std=c++17 -O2 -static qa-controller.cpp -o qa-controller
file qa-controller
sha256sum qa-controller
