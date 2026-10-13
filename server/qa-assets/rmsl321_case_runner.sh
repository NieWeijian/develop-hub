#!/bin/bash
# Single-case adapter for the RMSL321 factory-V2.0.4 QA package.
# It intentionally delegates the detailed checks and log layout to the package's
# vetted rmsl_single_module_test script. Device/algo baselines may run as setup,
# but exactly one requested test item is enabled per invocation.

set -euo pipefail

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
SUITE="$ROOT/rmsl_single_module_test"
CTL="$ROOT/bin/uvc_host_ctl"
STAMP="$(date +%Y%m%d_%H%M%S)"

usage() {
    cat <<'EOF'
Usage: develop_hub_case_runner CASE [CASE_ARG]

Cases:
  device-check              UVC RGB/DEPTH/IR enumeration
  projector-on              Turn the speckle projector on
  projector-off             Turn the speckle projector off
  usb-profile [ROUNDS]      high -> medium -> low -> high, default 1
  fov-switch [ROUNDS]       traverse FOV list and restore, default 1
  ctl-channel               full control-channel surface (no restart-app)
  stream [SECONDS]          RGB+DEPTH/IR stream monitor, default 30
  algo-switch [ROUNDS]      640x352 <-> 512x448, default 1
  device-mode [ROUNDS]      rgb+depth <-> rgb+ir, default 1
  isp-3a [ROUNDS]           ISP 3A stress, default 10
  factory-quick             factory test --quick --json
  stability [SECONDS]       stream + IMU + ctl soak, default 300
  ota-ab                    REAL NAND WRITE: A/B oem,system,boot from bin/update_img
EOF
}

[ -x "$SUITE" ] || { echo "missing suite: $SUITE" >&2; exit 2; }
[ -x "$CTL" ] || { echo "missing control tool: $CTL" >&2; exit 2; }

case_name="${1:-}"
case_arg="${2:-}"
[ -n "$case_name" ] || { usage >&2; exit 2; }
[ "$case_name" != "help" ] || { usage; exit 0; }

report_dir="$ROOT/logs/develop_hub/${case_name}_${STAMP}_$$"
common=(
    run --quick
    --no-check --no-profile --no-fov --no-ctl
    --stream-only --isp-only
    --no-switch --no-ft --no-ota --no-stability
    -o "$report_dir"
)

run_suite() {
    echo "CASE: $case_name"
    echo "REPORT_DIR: $report_dir"
    "$SUITE" "$@"
}

without() {
    local needle="$1" index
    for index in "${!args[@]}"; do
        if [ "${args[$index]}" = "$needle" ]; then
            unset 'args[index]'
        fi
    done
}

case "$case_name" in
    device-check)
        args=("${common[@]}")
        without --no-check
        run_suite "${args[@]}"
        ;;
    projector-on|projector-off)
        value="${case_name#projector-}"
        echo "CASE: $case_name"
        "$CTL" set-projector "$value"
        "$CTL" ping 1
        echo "RESULT: PASS (projector=$value)"
        ;;
    usb-profile)
        rounds="${case_arg:-1}"
        args=("${common[@]}")
        without --no-profile
        run_suite "${args[@]}" --profile-rounds "$rounds"
        ;;
    fov-switch)
        rounds="${case_arg:-1}"
        args=("${common[@]}")
        without --no-fov
        run_suite "${args[@]}" --fov-rounds "$rounds"
        ;;
    ctl-channel)
        args=("${common[@]}")
        without --no-ctl
        run_suite "${args[@]}"
        ;;
    stream)
        seconds="${case_arg:-30}"
        args=("${common[@]}")
        without --isp-only
        run_suite "${args[@]}" --stream-sec "$seconds"
        ;;
    algo-switch)
        rounds="${case_arg:-1}"
        args=("${common[@]}")
        without --no-switch
        run_suite "${args[@]}" --switch-rounds "$rounds" --mode-rounds 0
        ;;
    device-mode)
        rounds="${case_arg:-1}"
        args=("${common[@]}")
        without --no-switch
        run_suite "${args[@]}" --switch-rounds 0 --mode-rounds "$rounds"
        ;;
    isp-3a)
        rounds="${case_arg:-10}"
        args=("${common[@]}")
        without --stream-only
        run_suite "${args[@]}" --isp-rounds "$rounds"
        ;;
    factory-quick)
        args=("${common[@]}")
        without --no-ft
        run_suite "${args[@]}"
        ;;
    stability)
        seconds="${case_arg:-300}"
        args=("${common[@]}")
        without --no-stability
        run_suite "${args[@]}" --stability-sec "$seconds"
        ;;
    ota-ab)
        args=("${common[@]}")
        without --no-ota
        run_suite "${args[@]}" --firmware-dir "$ROOT/bin/update_img"
        ;;
    *)
        echo "unknown case: $case_name" >&2
        usage >&2
        exit 2
        ;;
esac
