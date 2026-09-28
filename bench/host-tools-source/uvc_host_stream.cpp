/*
 * Copyright (C) 2026 Rockchip Electronics Co., Ltd.
 *
 * This software is available to you under a choice of one of two
 * licenses.  You may choose to be licensed under the terms of the GNU
 * General Public License (GPL), available from the file
 * COPYING in the main directory of this source tree, or the
 * OpenIB.org BSD license below:
 *
 *     Redistribution and use in source and binary forms, with or
 *     without modification, are permitted provided that the following
 *     conditions are met:
 *
 *      - Redistributions of source code must retain the above
 *        copyright notice, this list of conditions and the following
 *        disclaimer.
 *
 *      - Redistributions in binary form must reproduce the above
 *        copyright notice, this list of conditions and the following
 *        disclaimer in the documentation and/or other materials
 *        provided with the distribution.
 *
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
 * EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
 * MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
 * NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS
 * BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
 * ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
 * CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
 * SOFTWARE.
 */

/*
 * Host-side RMSL321 stream monitor (fps / embedded sequence drops).
 * Mode arg: rgb+depth (default) | rgb+ir | rgb+depth+ir.
 * (`--depth` / `--ir` / `--triple` aliases also accepted.)
 * Stream capture ONLY — all control operations live in uvc_host_ctl.
 *
 * Build (from tools/):
 *   cmake -B build && cmake --build build
 *   ./build/uvc_host_stream
 */

#include "uvc_device.hpp"
#include "v4l2_capture.hpp"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <csignal>
#include <cstdint>
#include <cstring>
#include <iomanip>
#include <iostream>
#include <linux/videodev2.h>
#include <optional>
#include <string>
#include <system_error>
#include <thread>
#include <vector>

using Clock = std::chrono::steady_clock;
using TimePoint = Clock::time_point;

namespace {

constexpr uint16_t kUvcHeadMagic = 0xA55A;
constexpr uint16_t kFrameTypeRgb = 1;
constexpr uint16_t kFrameTypeDepth = 2;
constexpr uint16_t kFrameTypeIr = 3; /* combined IR (kTypeCombine) */

constexpr int kRgbWidth = 640;
constexpr int kRgbHeight = 540;
constexpr int kDepthWidth = 640;
constexpr int kDepthHeight = 352;
constexpr int kIrWidth = 540;
constexpr int kIrHeight = 1280;
constexpr int kTargetFps = 25; /* RK3572 standard frame rate (1126b used 10) */
constexpr int kBufferCount = 16;
constexpr int kStatsIntervalSec = 5;
constexpr auto kStatsInterval = std::chrono::seconds(kStatsIntervalSec);
constexpr auto kNoFrameWarnInterval = std::chrono::seconds(3);
constexpr auto kPollTimeout = std::chrono::milliseconds(200);

/* Common prefix shared by UvcHeadV1 and UvcHeadV2. */
#pragma pack(push, 1)
struct UvcHeadCommon {
    uint16_t magic;
    uint16_t version;
    uint16_t size;
    uint16_t type;
    uint16_t width;
    uint16_t height;
    int32_t sequence;
    int64_t timestamp;
    float temperature;
};
#pragma pack(pop)

struct StreamConfig {
    std::string label;
    std::string suffix;
    uvc_tools::CaptureFormat format;
    uint16_t expected_type = 0;
};

struct StatsCheckpoint {
    TimePoint time{};
    uint64_t frames = 0;
    uint64_t drops = 0;
    int32_t sequence = -1;
    bool initialized = false;
};

struct StreamContext {
    StreamConfig cfg;
    uvc_tools::V4l2Capture capture;
    std::atomic<bool> running{false};
    std::atomic<bool> header_missing_warned{false};
    std::atomic<uint64_t> frame_count{0};
    std::atomic<uint64_t> drop_count{0};
    std::atomic<int32_t> last_sequence{-1};
    std::thread worker;
};

std::atomic<bool> g_stop{false};
/* Pin the module: when set, RGB uses this node and DEPTH is derived from the
 * same USB device (multi-module hubs). */
std::string g_primary_device;

void log_error(std::string const &where, int err = errno) {
    std::cerr << where << ": " << std::system_error(err, std::generic_category()).what() << '\n';
}

std::optional<UvcHeadCommon> parse_frame_header(uvc_tools::MappedBuffer const &region, size_t bytes) {
    if (!region || bytes < sizeof(UvcHeadCommon))
        return std::nullopt;

    UvcHeadCommon head{};
    std::memcpy(&head, region.data(), sizeof(head));
    if (head.magic != kUvcHeadMagic)
        return std::nullopt;
    if (head.size < sizeof(UvcHeadCommon) || head.size > bytes)
        return std::nullopt;
    return head;
}

void update_sequence_stats(StreamContext &ctx, int32_t sequence) {
    int32_t const last = ctx.last_sequence.load();
    if (last >= 0) {
        if (sequence == last)
            return;

        if (sequence > last + 1) {
            ctx.drop_count.fetch_add(static_cast<uint64_t>(sequence - last - 1));
            std::cerr << '[' << ctx.cfg.label << "] WARN: sequence gap (" << last << " -> "
                      << sequence << "), drop +" << sequence - last - 1 << '\n';
        } else if (sequence < last) {
            std::cerr << '[' << ctx.cfg.label << "] INFO: seq reset (" << last << " -> "
                      << sequence << ")\n";
        }
    }

    ctx.last_sequence.store(sequence);
    ctx.frame_count.fetch_add(1);
}

void print_stream_stats(StreamContext const &ctx, StatsCheckpoint &last) {
    TimePoint const now = Clock::now();
    if (!last.initialized) {
        last.time = now;
        last.frames = ctx.frame_count.load();
        last.drops = ctx.drop_count.load();
        last.sequence = ctx.last_sequence.load();
        last.initialized = true;
        return;
    }

    auto const elapsed = now - last.time;
    if (elapsed < kStatsInterval)
        return;

    uint64_t const frames = ctx.frame_count.load();
    uint64_t const drops = ctx.drop_count.load();
    int32_t const seq = ctx.last_sequence.load();
    uint64_t const interval_recv = frames - last.frames;
    uint64_t const interval_drops = drops - last.drops;
    auto const elapsed_us = std::chrono::duration_cast<std::chrono::microseconds>(elapsed).count();
    double const fps =
        elapsed_us > 0 ? interval_recv * 1e6 / static_cast<double>(elapsed_us) : 0.0;

    std::cout << '[' << ctx.cfg.label << "] " << kStatsIntervalSec << "s: fps=" << std::fixed
              << std::setprecision(2) << fps;
    if (last.sequence >= 0)
        std::cout << " seq=" << last.sequence << "->" << seq;
    else
        std::cout << " seq=" << seq;
    std::cout << " recv=" << interval_recv << " drops_by_seq=" << drops << "(+" << interval_drops
              << ") total=" << frames << '\n';

    last.time = now;
    last.frames = frames;
    last.drops = drops;
    last.sequence = seq;
}

void handle_frame(StreamContext &ctx, v4l2_buffer const &buf) {
    size_t const frame_bytes = buf.bytesused > 0 ? buf.bytesused : buf.length;
    auto const head = parse_frame_header(ctx.capture.buffer(buf.index), frame_bytes);
    if (!head) {
        /* Firmware may not include the custom UVC header; fall back to the
         * V4L2 buffer sequence and warn only once. */
        if (!ctx.header_missing_warned.exchange(true))
            std::cerr << '[' << ctx.cfg.label
                      << "] WARN: module frame header missing, using v4l2 seq "
                         "(reported once)\n";
        update_sequence_stats(ctx, static_cast<int32_t>(buf.sequence));
        return;
    }

    if (head->type != ctx.cfg.expected_type) {
        std::cerr << '[' << ctx.cfg.label << "] WARN: unexpected frame type " << head->type
                  << " (expect " << ctx.cfg.expected_type << ")\n";
    }
    update_sequence_stats(ctx, head->sequence);
}

void stream_loop(StreamContext &ctx) {
    StatsCheckpoint last_stats;
    TimePoint const start_time = Clock::now();
    std::optional<TimePoint> last_no_frame_warn;
    uint64_t raw_buf_count = 0;

    while (!g_stop.load() && ctx.running.load()) {
        if (!ctx.capture.poll_readable(kPollTimeout)) {
            print_stream_stats(ctx, last_stats);
            continue;
        }

        while (!g_stop.load() && ctx.running.load()) {
            v4l2_buffer buf{};
            try {
                if (!ctx.capture.try_dequeue(buf))
                    break;
            } catch (std::system_error const &ex) {
                log_error(ctx.cfg.label + ": VIDIOC_DQBUF", ex.code().value());
                ctx.running.store(false);
                return;
            }

            ++raw_buf_count;
            handle_frame(ctx, buf);

            try {
                ctx.capture.queue(buf);
            } catch (std::system_error const &ex) {
                std::cerr << ctx.cfg.label << ": VIDIOC_QBUF: " << ex.what() << '\n';
                ctx.running.store(false);
                return;
            }
        }

        print_stream_stats(ctx, last_stats);

        if (raw_buf_count == 0) {
            TimePoint const now = Clock::now();
            if (now - start_time > kNoFrameWarnInterval &&
                (!last_no_frame_warn || now - *last_no_frame_warn > kNoFrameWarnInterval)) {
                std::cerr << '[' << ctx.cfg.label
                          << "] WARN: no frames from driver, check device app (dualCamDis)\n";
                last_no_frame_warn = now;
            }
        }
    }

    ctx.running.store(false);
}

bool open_and_prepare(StreamContext &ctx) {
    std::string dev_path;
    if (!g_primary_device.empty()) {
        if (ctx.cfg.suffix == "RGB") {
            dev_path = g_primary_device;
        } else if (auto sibling = uvc_tools::sibling_video_node(
                       g_primary_device, "RMSL321_" + ctx.cfg.suffix)) {
            dev_path = *sibling;
        }
    } else {
        /* The module reports RMSL321_* normally and RMSL322_* when the sensor
         * detection falls back; accept both. */
        auto const found = uvc_tools::find_capture_device_any(
            {"RMSL321_" + ctx.cfg.suffix, "RMSL322_" + ctx.cfg.suffix});
        if (found)
            dev_path = *found;
    }
    if (dev_path.empty()) {
        std::cerr << "ERROR: cannot find V4L2 device matching 'RMSL32"
                  << ctx.cfg.suffix << "'\n";
        return false;
    }

    if (!ctx.capture.open(dev_path) || !ctx.capture.configure(ctx.cfg.format, kBufferCount) ||
        !ctx.capture.start()) {
        ctx.capture.close();
        return false;
    }

    if (ctx.capture.width() != ctx.cfg.format.width ||
        ctx.capture.height() != ctx.cfg.format.height) {
        std::cerr << '[' << ctx.cfg.label << "] WARN: driver adjusted resolution to "
                  << ctx.capture.width() << 'x' << ctx.capture.height() << " (requested "
                  << ctx.cfg.format.width << 'x' << ctx.cfg.format.height << ")\n";
    }

    std::cout << '[' << ctx.cfg.label << "] streaming " << ctx.capture.width() << 'x'
              << ctx.capture.height() << '@' << ctx.cfg.format.fps << "fps from "
              << ctx.capture.device_path() << '\n';
    return true;
}

StreamConfig make_rgb_config() {
    return StreamConfig{
        "RMSL32_RGB",
        "RGB",
        {V4L2_PIX_FMT_NV12, kRgbWidth, kRgbHeight, kTargetFps},
        kFrameTypeRgb,
    };
}

StreamConfig make_depth_config() {
    return StreamConfig{
        "RMSL32_DEPTH",
        "DEPTH",
        {V4L2_PIX_FMT_YUYV, kDepthWidth, kDepthHeight, kTargetFps},
        kFrameTypeDepth,
    };
}

StreamConfig make_ir_config() {
    return StreamConfig{
        "RMSL32_IR",
        "IR",
        {V4L2_PIX_FMT_NV12, kIrWidth, kIrHeight, kTargetFps},
        kFrameTypeIr,
    };
}

} // namespace

int main(int argc, char **argv) {
    enum class Mode { Depth, Ir, Triple };
    Mode mode = Mode::Depth;
    for (int i = 1; i < argc; ++i) {
        std::string const a = argv[i];
        if ((a == "--device" || a == "--rgb-node") && i + 1 < argc) {
            g_primary_device = argv[++i];
        } else if (a == "--ir" || a == "rgb+ir") {
            mode = Mode::Ir;
        } else if (a == "--triple" || a == "rgb+depth+ir") {
            mode = Mode::Triple;
        } else if (a == "--depth" || a == "rgb+depth") {
            mode = Mode::Depth;
        } else if (a == "--help" || a == "-h") {
            std::cout << "Usage: " << argv[0] << " [--device NODE|--rgb-node NODE]\n"
                      << "  rgb+depth        RGB + DEPTH (default)\n"
                      << "  rgb+ir           RGB + IR\n"
                      << "  rgb+depth+ir     RGB + DEPTH + IR\n"
                      << "  (--depth / --ir / --triple aliases also accepted)\n";
            return 0;
        }
    }

    std::signal(SIGINT, +[](int) { g_stop.store(true); });
    std::signal(SIGTERM, +[](int) { g_stop.store(true); });

    StreamContext rgb_ctx;
    StreamContext second_ctx;
    StreamContext ir_ctx;
    rgb_ctx.cfg = make_rgb_config();
    second_ctx.cfg = (mode == Mode::Ir) ? make_ir_config() : make_depth_config();
    if (mode == Mode::Triple)
        ir_ctx.cfg = make_ir_config();

    if (!open_and_prepare(rgb_ctx)) {
        rgb_ctx.capture.close();
        return 1;
    }
    if (!open_and_prepare(second_ctx)) {
        rgb_ctx.capture.close();
        second_ctx.capture.close();
        return 1;
    }
    if (mode == Mode::Triple && !open_and_prepare(ir_ctx)) {
        rgb_ctx.capture.close();
        second_ctx.capture.close();
        ir_ctx.capture.close();
        return 1;
    }

    rgb_ctx.running.store(true);
    second_ctx.running.store(true);
    if (mode == Mode::Triple)
        ir_ctx.running.store(true);
    rgb_ctx.worker = std::thread(stream_loop, std::ref(rgb_ctx));
    second_ctx.worker = std::thread(stream_loop, std::ref(second_ctx));
    if (mode == Mode::Triple)
        ir_ctx.worker = std::thread(stream_loop, std::ref(ir_ctx));

    while (!g_stop.load()) {
        if (!rgb_ctx.running.load() || !second_ctx.running.load() ||
            (mode == Mode::Triple && !ir_ctx.running.load()))
            break;
        std::this_thread::sleep_for(std::chrono::seconds(1));
    }

    g_stop.store(true);
    rgb_ctx.running.store(false);
    second_ctx.running.store(false);
    if (mode == Mode::Triple)
        ir_ctx.running.store(false);

    if (rgb_ctx.worker.joinable())
        rgb_ctx.worker.join();
    if (second_ctx.worker.joinable())
        second_ctx.worker.join();
    if (mode == Mode::Triple && ir_ctx.worker.joinable())
        ir_ctx.worker.join();

    rgb_ctx.capture.close();
    second_ctx.capture.close();
    if (mode == Mode::Triple)
        ir_ctx.capture.close();

    std::cout << "stopped.\n";
    return 0;
}
