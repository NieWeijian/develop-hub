#!/bin/bash
set -euo pipefail

readonly REQUIRED_FRAMES=30
readonly CAPTURE_FRAMES=60
readonly RGB_FRAME_SIZE=$((640 * 540 * 3 / 2))
readonly DEPTH_FRAME_SIZE=$((640 * 352 * 2))
readonly VIDEO4LINUX_SYSFS_ROOT="${RMSL_QA_SYSFS_ROOT:-/sys/class/video4linux}"
readonly VIDEO_DEVICE_ROOT="${RMSL_QA_DEV_ROOT:-/dev}"

for command_name in v4l2-ctl timeout od stat tr basename dirname mktemp rm sed; do
    if ! command -v "${command_name}" >/dev/null 2>&1; then
        echo "ERROR: required command not found: ${command_name}" >&2
        exit 2
    fi
done

is_capture_streaming_node() {
    local node="$1"
    local capability_output line caps="" fallback_caps=""
    local caps_value

    if ! capability_output=$(v4l2-ctl -D -d "${node}" 2>/dev/null); then
        return 1
    fi

    while IFS= read -r line; do
        case "${line}" in
            *"Device Caps"*)
                caps=${line##*:}
                break
                ;;
            *"Capabilities"*)
                fallback_caps=${line##*:}
                ;;
        esac
    done <<< "${capability_output}"

    caps=${caps:-${fallback_caps}}
    caps=${caps//[[:space:]]/}
    [[ "${caps}" =~ ^0[xX][0-9a-fA-F]+$ ]] || return 1
    caps_value=$((caps))
    if (( caps_value == 0 )); then
        fallback_caps=${fallback_caps//[[:space:]]/}
        [[ "${fallback_caps}" =~ ^0[xX][0-9a-fA-F]+$ ]] || return 1
        caps_value=$((fallback_caps))
    fi

    # Match tools/common/v4l2_capture.cpp: require VIDEO_CAPTURE + STREAMING.
    (( (caps_value & 0x00000001) != 0 && (caps_value & 0x04000000) != 0 ))
}

find_unique_node() {
    local role="$1"
    local -a matches=()
    local -a named_matches=()
    local name_path name_upper candidate

    for name_path in "${VIDEO4LINUX_SYSFS_ROOT}"/video*/name; do
        [[ -r "${name_path}" ]] || continue
        name_upper=$(tr '[:lower:]' '[:upper:]' < "${name_path}")
        if [[ "${name_upper}" == *RMSL*"${role}"* ]]; then
            candidate="${VIDEO_DEVICE_ROOT}/$(basename "$(dirname "${name_path}")")"
            named_matches+=("${candidate}")
            if is_capture_streaming_node "${candidate}"; then
                matches+=("${candidate}")
            fi
        fi
    done

    if (( ${#matches[@]} != 1 )); then
        echo "ERROR: found ${#named_matches[@]} RMSL ${role} named nodes, but expected exactly one capture+streaming node; found ${#matches[@]}" >&2
        if (( ${#named_matches[@]} > 0 )); then
            echo "Named candidates:" >&2
            printf '  %s\n' "${named_matches[@]}" >&2
        fi
        if (( ${#matches[@]} > 0 )); then
            echo "Capture+streaming candidates:" >&2
            printf '  %s\n' "${matches[@]}" >&2
        fi
        return 1
    fi
    printf '%s\n' "${matches[0]}"
}

RGB_NODE=$(find_unique_node RGB)
DEPTH_NODE=$(find_unique_node DEPTH)
WORK_DIR=$(mktemp -d /tmp/rmsl-frame-header-exposure.XXXXXX)
readonly RGB_NODE DEPTH_NODE WORK_DIR
readonly RGB_CAPTURE="${WORK_DIR}/rgb.raw"
readonly DEPTH_CAPTURE="${WORK_DIR}/depth.raw"
declare -a CAPTURE_PIDS=()

cleanup() {
    local pid
    for pid in "${CAPTURE_PIDS[@]}"; do
        kill "${pid}" >/dev/null 2>&1 || true
    done
    case "${WORK_DIR}" in
        /tmp/rmsl-frame-header-exposure.*) rm -rf -- "${WORK_DIR}" ;;
    esac
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

echo "RGB node: ${RGB_NODE}"
echo "DEPTH node: ${DEPTH_NODE}"

timeout 45s v4l2-ctl -d "${RGB_NODE}" \
    --set-fmt-video=width=640,height=540,pixelformat=NV12 \
    --set-parm=10 --stream-mmap=4 --stream-poll \
    --stream-count="${CAPTURE_FRAMES}" --stream-to="${RGB_CAPTURE}" \
    >"${WORK_DIR}/rgb.log" 2>&1 &
CAPTURE_PIDS+=("$!")

timeout 45s v4l2-ctl -d "${DEPTH_NODE}" \
    --set-fmt-video=width=640,height=352,pixelformat=YUYV \
    --set-parm=10 --stream-mmap=4 --stream-poll \
    --stream-count="${CAPTURE_FRAMES}" --stream-to="${DEPTH_CAPTURE}" \
    >"${WORK_DIR}/depth.log" 2>&1 &
CAPTURE_PIDS+=("$!")

capture_failed=0
for pid in "${CAPTURE_PIDS[@]}"; do
    if ! wait "${pid}"; then
        capture_failed=1
    fi
done
CAPTURE_PIDS=()

if (( capture_failed != 0 )); then
    echo "ERROR: paired stream capture failed" >&2
    sed -n '1,120p' "${WORK_DIR}/rgb.log" >&2
    sed -n '1,120p' "${WORK_DIR}/depth.log" >&2
    exit 3
fi

check_headers() {
    local label="$1"
    local capture_file="$2"
    local frame_size="$3"
    local expected_type="$4"
    local file_size frame_count index offset
    local magic version head_size frame_type exposure
    local consecutive=0 max_consecutive=0 min_exposure=0 max_exposure=0
    local -a bytes

    if [[ ! -s "${capture_file}" ]]; then
        echo "ERROR: ${label} capture is empty" >&2
        return 1
    fi
    file_size=$(stat -c '%s' "${capture_file}")
    frame_count=$((file_size / frame_size))
    if (( frame_count < REQUIRED_FRAMES )); then
        echo "ERROR: ${label} captured ${frame_count} complete frames, need ${REQUIRED_FRAMES}" >&2
        return 1
    fi

    for ((index = 0; index < frame_count; ++index)); do
        offset=$((index * frame_size))
        read -r -a bytes <<< "$(od -An -v -tu1 -N36 -j "${offset}" "${capture_file}" | tr '\n' ' ')"
        if (( ${#bytes[@]} != 36 )); then
            consecutive=0
            continue
        fi

        magic=$((bytes[0] | (bytes[1] << 8)))
        version=$((bytes[2] | (bytes[3] << 8)))
        head_size=$((bytes[4] | (bytes[5] << 8)))
        frame_type=$((bytes[6] | (bytes[7] << 8)))
        exposure=$((bytes[28] | (bytes[29] << 8) | (bytes[30] << 16) | (bytes[31] << 24)))

        if (( magic == 0xA55A && version == 2 && head_size >= 36 && \
              head_size <= frame_size && frame_type == expected_type && exposure > 0 )); then
            ((consecutive += 1))
            (( consecutive > max_consecutive )) && max_consecutive=${consecutive}
            if (( min_exposure == 0 || exposure < min_exposure )); then
                min_exposure=${exposure}
            fi
            (( exposure > max_exposure )) && max_exposure=${exposure}
        else
            consecutive=0
        fi
    done

    if (( max_consecutive < REQUIRED_FRAMES )); then
        echo "ERROR: ${label} has only ${max_consecutive} consecutive valid non-zero exposure headers; need ${REQUIRED_FRAMES}" >&2
        return 1
    fi

    echo "PASS: ${label} ${max_consecutive} consecutive headers, exposure_us=${min_exposure}..${max_exposure}"
}

check_headers RGB "${RGB_CAPTURE}" "${RGB_FRAME_SIZE}" 1
check_headers DEPTH "${DEPTH_CAPTURE}" "${DEPTH_FRAME_SIZE}" 2
echo "RESULT: PASS"
