#!/bin/sh
# stream_ctl_latency.sh [rounds] [ping_count]
# SL-T03-09: 流传输期间控制命令延时（uvc_host_preview 出流稳定后执行 uvc_host_ctl ping）
#
# 步骤:
#   1. 空载基线: ping N x2 轮
#   2. 启动 uvc_host_preview (rgb+depth 640x540 + 640x352@10), 等出流稳定(出现 fps 行后 +5s)
#   3. 流中: ping N x ROUNDS 轮（出流已稳定，不丢弃任何包，全量统计）
#   4. SIGINT 优雅停预览, 汇总判定
#
# 判定: 无失败; min RTT <= 5ms; avg RTT <= 100ms
# 环境: CTL_DIR/PREVIEW/CTL 可覆盖; 预览停启间隔 >=10s, 防测试台 xHCI 挂死
CTL_DIR=${CTL_DIR:-/home/linaro/Desktop/DEV-structlight/tools/build-factory-V2.0.2}
PREVIEW=${PREVIEW:-$CTL_DIR/uvc_host_preview}
CTL=${CTL:-$CTL_DIR/uvc_host_ctl}
ROUNDS=${1:-3}
N=${2:-10}
STABLE_WAIT=${STABLE_WAIT:-20}
TS=$(date +%Y%m%d-%H%M%S)
LOG=/tmp/stream_ctl_latency_$TS.log
PREVIEW_LOG=/tmp/preview_ctl_latency.log
: > "$LOG"

echo "SL-T03-09 stream control latency test @ $TS (rounds=$ROUNDS ping=$N, no discard)" | tee -a "$LOG"

run_ping() {
    label=$1; n=$2
    tmp=$(mktemp)
    timeout 60 "$CTL" ping "$n" > "$tmp" 2>&1
    echo "== $label ==" >> "$LOG"
    cat "$tmp" >> "$LOG"
    grep "summary:" "$tmp" | sed "s/^/[$label raw] /" | tee -a "$LOG"
    awk -v label="$label" '
        /^ping [0-9]+:/ {
            v = $3 + 0
            if (c == 0) { first = v; min = v; max = v; sum = v; c = 1; next }
            if (v < min) min = v
            if (v > max) max = v
            sum += v; c++
        }
        END {
            if (c > 0)
                printf "%s: first=%s ms min=%.2f avg=%.2f max=%.2f ms (n=%d)\n", label, first, min, sum / c, max, c
            else
                print label ": no data"
        }' "$tmp" | tee -a "$LOG"
    rm -f "$tmp"
}

# 1) idle baseline
run_ping "IDLE baseline round 1" "$N"
run_ping "IDLE baseline round 2" "$N"

# 2) start preview, wait until stable (fps line printed) + settle
rm -f "$PREVIEW_LOG"
"$PREVIEW" > "$PREVIEW_LOG" 2>&1 &
PID=$!
for i in $(seq 1 "$STABLE_WAIT"); do
    grep -q "^preview: .* fps" "$PREVIEW_LOG" 2>/dev/null && break
    sleep 1
done
if ! grep -q "^preview: .* fps" "$PREVIEW_LOG" 2>/dev/null; then
    echo "FAIL: preview not streaming" | tee -a "$LOG"
    tail -5 "$PREVIEW_LOG" | tee -a "$LOG"
    kill -INT "$PID" 2>/dev/null
    exit 1
fi
echo "preview stable (pid=$PID)" | tee -a "$LOG"
sleep 5

# 3) during stream: measure all pings (stream already stable, no discard)
r=1
while [ "$r" -le "$ROUNDS" ]; do
    run_ping "STREAM round $r" "$N"
    r=$((r + 1))
done

# 4) stop preview (SIGINT), wait before any next run
kill -INT "$PID" 2>/dev/null
sleep 10

# 5) summary / judgement (full stats, no discard)
echo "=== preview log tail ===" | tee -a "$LOG"
tail -5 "$PREVIEW_LOG" | tee -a "$LOG"

FAILS=$(grep -cE "failed|timeout|control command failed" "$LOG")
STREAM_AVG=$(grep -E "STREAM round [0-9]+: first=" "$LOG" | sed -n 's/.*avg=\([0-9.]*\) .*/\1/p' | sort -n | tail -1)
STREAM_MIN=$(grep -E "STREAM round [0-9]+: first=" "$LOG" | sed -n 's/.*min=\([0-9.]*\) .*/\1/p' | sort -n | head -1)
STREAM_MAX=$(grep -E "STREAM round [0-9]+: first=" "$LOG" | sed -n 's/.*max=\([0-9.]*\) .*/\1/p' | sort -n | tail -1)
IDLE_MIN=$(grep -E "IDLE baseline round [0-9]+: first=" "$LOG" | sed -n 's/.*min=\([0-9.]*\) .*/\1/p' | sort -n | head -1)

echo "== result (full stats, no discard) ==" | tee -a "$LOG"
echo "idle min RTT   : $IDLE_MIN ms" | tee -a "$LOG"
echo "stream min RTT : $STREAM_MIN ms (criterion <= 5)" | tee -a "$LOG"
echo "stream avg RTT : $STREAM_AVG ms (criterion <= 100)" | tee -a "$LOG"
echo "stream max RTT : $STREAM_MAX ms" | tee -a "$LOG"
echo "failures       : $FAILS" | tee -a "$LOG"
if [ -z "$STREAM_MIN" ] || [ -z "$STREAM_AVG" ]; then
    echo "RESULT: FAIL (missing data)" | tee -a "$LOG"
    exit 1
fi
if [ "$FAILS" -gt 0 ] || [ "$(echo "$STREAM_MIN > 5" | bc 2>/dev/null)" = "1" ] || [ "$(echo "$STREAM_AVG > 100" | bc 2>/dev/null)" = "1" ]; then
    echo "RESULT: FAIL" | tee -a "$LOG"
    exit 1
fi
echo "RESULT: PASS" | tee -a "$LOG"
echo "log: $LOG"