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
 * Host-side RMSL321 live preview on DRM (single frame).
 * Default shows DEPTH; --rgb / --ir pick RGB or IR instead. RGB is always
 * opened first (IR/Depth depend on its FSYNC), even when not displayed.
 *
 * Build (from tools/):
 *   cmake -B build && cmake --build build
 *   ./build/uvc_host_preview [--device PATH]
 */

#include "drm_preview.hpp"
#include "uvc_device.hpp"
#include "v4l2_capture.hpp"

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <csignal>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <linux/videodev2.h>
#include <mutex>
#include <optional>
#include <string>
#include <system_error>
#include <thread>

using Clock = std::chrono::steady_clock;
using TimePoint = Clock::time_point;

namespace {

constexpr uint16_t kUvcHeadMagic = 0xA55A;
constexpr uint16_t kFrameTypeRgb = 1;
constexpr uint16_t kFrameTypeDepth = 2;
constexpr uint16_t kFrameTypeIr = 3;

constexpr int kRgbWidth = 640;
constexpr int kRgbHeight = 540;
constexpr int kDepthWidth = 640;
constexpr int kDepthHeight = 352;
constexpr int kIrWidth = 540;
constexpr int kIrHeight = 1280;
constexpr int kTargetFps = 25; /* RK3572 standard frame rate (1126b used 10) */
constexpr int kBufferCount = 4;
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
    std::string name_match;
    uvc_tools::CaptureFormat format;
    uint16_t expected_type = 0;
};

struct StreamContext {
    StreamConfig cfg;
    uvc_tools::V4l2Capture capture;
    std::atomic<bool> running{false};
    std::thread worker;
    bool render = true; /* submit frames to the preview hub or only drain */
};

struct AppOptions {
    std::string drm_device = "/dev/dri/by-path/platform-display-subsystem-card";
    int rgb_width = kRgbWidth;
    int rgb_height = kRgbHeight;
    int depth_width = kDepthWidth;
    int depth_height = kDepthHeight;
    std::string mode = "depth"; /* depth | rgb | ir */
};

class PreviewHub {
public:
    void set_enabled(bool enabled) {
        {
            std::lock_guard<std::mutex> lock(mutex_);
            enabled_ = enabled;
            if (!enabled_) {
                frame_ = {};
            }
        }
        cv_.notify_all();
    }

    void submit(uvc_tools::PreviewFrame frame) {
        {
            std::lock_guard<std::mutex> lock(mutex_);
            if (!enabled_)
                return;
            frame_ = std::move(frame);
            frame_.valid = true;
        }
        cv_.notify_one();
    }

    bool wait_latest(uvc_tools::PreviewFrame &frame,
                          std::atomic<bool> const &stop) {
        std::unique_lock<std::mutex> lock(mutex_);
        cv_.wait(lock, [&] { return stop.load() || !enabled_ || frame_.valid; });
        if (stop.load() || !enabled_)
            return false;
        frame = std::move(frame_);
        frame_.valid = false;
        return true;
    }

private:
    std::mutex mutex_;
    std::condition_variable cv_;
    uvc_tools::PreviewFrame frame_;
    bool enabled_ = false;
};

std::atomic<bool> g_stop{false};
/* Pin the module: RGB uses this node, the second panel (DEPTH/IR) is derived
 * from the same USB device. */
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

void submit_preview_frame(PreviewHub &hub, StreamContext &ctx, v4l2_buffer const &buf) {
    size_t const frame_bytes = buf.bytesused > 0 ? buf.bytesused : buf.length;
    auto const head = parse_frame_header(ctx.capture.buffer(buf.index), frame_bytes);
    size_t header_size = 0;
    if (head) {
        if (head->type != ctx.cfg.expected_type)
            return;
        header_size = head->size;
    }
    if (frame_bytes <= header_size)
        return;

    uvc_tools::PreviewFrame frame;
    frame.width = ctx.capture.width();
    frame.height = ctx.capture.height();
    auto const *payload = ctx.capture.buffer(buf.index).data() + header_size;
    size_t const payload_size = frame_bytes - header_size;
    if (frame.payload.size() != payload_size)
        frame.payload.resize(payload_size);
    std::memcpy(frame.payload.data(), payload, payload_size);
    frame.valid = true;

    if (ctx.render)
        hub.submit(std::move(frame));
}

void handle_frame(PreviewHub &hub, StreamContext &ctx, v4l2_buffer const &buf) {
    submit_preview_frame(hub, ctx, buf);
}

void stream_loop(StreamContext &ctx, PreviewHub &hub) {
    TimePoint const start_time = Clock::now();
    std::optional<TimePoint> last_no_frame_warn;
    bool got_frame = false;

    while (!g_stop.load() && ctx.running.load()) {
        if (!ctx.capture.poll_readable(kPollTimeout)) {
            if (!got_frame) {
                TimePoint const now = Clock::now();
                if (now - start_time > kNoFrameWarnInterval &&
                    (!last_no_frame_warn || now - *last_no_frame_warn > kNoFrameWarnInterval)) {
                    std::cerr << '[' << ctx.cfg.label << "] WARN: no frames, check dualCamDis\n";
                    last_no_frame_warn = now;
                }
            }
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

            got_frame = true;
            handle_frame(hub, ctx, buf);

            try {
                ctx.capture.queue(buf);
            } catch (std::system_error const &ex) {
                std::cerr << ctx.cfg.label << ": VIDIOC_QBUF: " << ex.what() << '\n';
                ctx.running.store(false);
                return;
            }
        }
    }

    ctx.running.store(false);
}

bool open_and_prepare(StreamContext &ctx) {
    std::string dev_path;
    if (!g_primary_device.empty()) {
        if (ctx.cfg.expected_type == kFrameTypeRgb) {
            dev_path = g_primary_device;
        } else {
            char const *suffix = (ctx.cfg.expected_type == kFrameTypeIr) ? "RMSL321_IR"
                                                                        : "RMSL321_DEPTH";
            if (auto const s = uvc_tools::sibling_video_node(g_primary_device, suffix))
                dev_path = *s;
        }
    } else {
        auto const found = uvc_tools::find_capture_device(ctx.cfg.name_match);
        if (found)
            dev_path = *found;
    }
    if (dev_path.empty()) {
        std::cerr << "ERROR: cannot find V4L2 device matching '" << ctx.cfg.name_match << "'\n";
        return false;
    }

    if (!ctx.capture.open(dev_path)) {
        std::cerr << "ERROR: cannot open " << dev_path << ": "
                  << std::system_error(errno, std::generic_category()).what() << '\n';
        return false;
    }
    if (!ctx.capture.configure(ctx.cfg.format, kBufferCount)) {
        uint32_t const fcc = ctx.cfg.format.pixfmt;
        char const fcc_str[5] = {
            static_cast<char>(fcc & 0xFF),
            static_cast<char>((fcc >> 8) & 0xFF),
            static_cast<char>((fcc >> 16) & 0xFF),
            static_cast<char>((fcc >> 24) & 0xFF),
            '\0',
        };
        std::cerr << "ERROR: " << ctx.cfg.label << ": cannot configure "
                  << ctx.cfg.format.width << 'x' << ctx.cfg.format.height << " (" << fcc_str
                  << ")\n";
        auto const sizes = ctx.capture.enumerate_frame_sizes(ctx.cfg.format.pixfmt);
        if (!sizes.empty()) {
            std::cerr << "  supported frame sizes:";
            for (auto const &[w, h] : sizes)
                std::cerr << ' ' << w << 'x' << h;
            std::cerr << '\n';
        } else {
            std::cerr << "  (device reported no matching frame sizes)\n";
        }
        ctx.capture.close();
        return false;
    }
    if (!ctx.capture.start()) {
        std::cerr << "ERROR: " << ctx.cfg.label << ": cannot start stream\n";
        ctx.capture.close();
        return false;
    }

    if (ctx.capture.width() != ctx.cfg.format.width ||
        ctx.capture.height() != ctx.cfg.format.height) {
        std::cerr << '[' << ctx.cfg.label << "] WARN: driver adjusted resolution to "
                  << ctx.capture.width() << 'x' << ctx.capture.height() << " (requested "
                  << ctx.cfg.format.width << 'x' << ctx.cfg.format.height << ")\n";
    }

    std::cout << '[' << ctx.cfg.label << "] " << ctx.capture.width() << 'x'
              << ctx.capture.height() << '@' << ctx.cfg.format.fps << "fps from "
              << ctx.capture.device_path() << '\n';
    return true;
}

bool parse_resolution(std::string const &text, int &width, int &height) {
    std::size_t const sep = text.find('x');
    if (sep == std::string::npos || sep == 0 || sep + 1 >= text.size())
        return false;
    try {
        width = std::stoi(text.substr(0, sep));
        height = std::stoi(text.substr(sep + 1));
    } catch (...) {
        return false;
    }
    return width > 0 && height > 0;
}

StreamConfig make_rgb_config(AppOptions const &opts) {
    return StreamConfig{
        "RMSL321_RGB",
        "RMSL321_RGB",
        {V4L2_PIX_FMT_NV12, opts.rgb_width, opts.rgb_height, kTargetFps},
        kFrameTypeRgb,
    };
}

StreamConfig make_depth_config(AppOptions const &opts) {
    return StreamConfig{
        "RMSL321_DEPTH",
        "RMSL321_DEPTH",
        {V4L2_PIX_FMT_YUYV, opts.depth_width, opts.depth_height, kTargetFps},
        kFrameTypeDepth,
    };
}

StreamConfig make_ir_config() {
    return StreamConfig{
        "RMSL321_IR",
        "RMSL321_IR",
        {V4L2_PIX_FMT_NV12, kIrWidth, kIrHeight, kTargetFps},
        kFrameTypeIr,
    };
}

void print_usage(char const *prog) {
    std::cout << "Usage: " << prog << " [options]\n"
              << "  --device PATH  DRM card path (default: "
                 "/dev/dri/by-path/platform-display-subsystem-card)\n"
              << "  (default)      preview DEPTH only\n"
              << "  --rgb [WxH]    preview RGB only (optional resolution)\n"
              << "  --depth WxH    DEPTH capture resolution (default: 640x352)\n"
              << "  --ir           preview IR only (RGB opened first as FSYNC master)\n"
              << "  --help         Show this help\n";
}

AppOptions parse_options(int argc, char **argv) {
    AppOptions opts;
    for (int i = 1; i < argc; ++i) {
        std::string const arg = argv[i];
        if ((arg == "--preview-device") && i + 1 < argc) {
            opts.drm_device = argv[++i];
        } else if (arg == "--device" && i + 1 < argc) {
            g_primary_device = argv[++i];
        } else if (arg == "--rgb") {
            opts.mode = "rgb";
            if (i + 1 < argc && parse_resolution(argv[i + 1], opts.rgb_width, opts.rgb_height))
                ++i;
        } else if (arg == "--depth" && i + 1 < argc) {
            if (!parse_resolution(argv[++i], opts.depth_width, opts.depth_height)) {
                std::cerr << "Invalid --depth resolution: " << argv[i] << '\n';
                std::exit(1);
            }
        } else if (arg == "--ir") {
            opts.mode = "ir";
        } else if (arg == "--help" || arg == "-h") {
            print_usage(argv[0]);
            std::exit(0);
        } else {
            std::cerr << "Unknown option: " << arg << '\n';
            print_usage(argv[0]);
            std::exit(1);
        }
    }
    return opts;
}

void preview_loop(PreviewHub &hub,
                  uvc_tools::DrmPreviewDisplay &display,
                  std::string const &mode,
                  std::atomic<bool> &stop) {
    uvc_tools::PreviewFrame frame;
    bool logged_present_error = false;
    unsigned long frames = 0;
    auto fps_t0 = std::chrono::steady_clock::now();
    while (!stop.load()) {
        if (!hub.wait_latest(frame, stop))
            break;
        bool const ok = (mode == "ir") ? display.present_ir_left_right(frame)
                                       : display.present_single(frame, mode == "depth");
        if (!ok) {
            if (!logged_present_error) {
                std::cerr << "preview: present failed (payload=" << frame.payload.size() << ")\n";
                logged_present_error = true;
            }
        } else {
            logged_present_error = false;
        }
        ++frames;
        auto const now = std::chrono::steady_clock::now();
        auto const elapsed_ms =
            std::chrono::duration_cast<std::chrono::milliseconds>(now - fps_t0).count();
        if (elapsed_ms >= 5000) {
            double const fps = static_cast<double>(frames) * 1000.0 / elapsed_ms;
            std::cerr << "preview: " << fps << " fps (" << frames << " frames in "
                      << elapsed_ms << " ms)\n";
            frames = 0;
            fps_t0 = now;
        }
    }
}

} // namespace

int main(int argc, char **argv) {
    AppOptions const opts = parse_options(argc, argv);

    std::signal(SIGINT, +[](int) { g_stop.store(true); });
    std::signal(SIGTERM, +[](int) { g_stop.store(true); });

    PreviewHub preview_hub;
    preview_hub.set_enabled(true);

    uvc_tools::DrmPreviewDisplay preview_display;
    if (!preview_display.init(opts.drm_device)) {
        std::cerr << "preview: init failed\n";
        return 1;
    }

    std::thread preview_thread(preview_loop, std::ref(preview_hub), std::ref(preview_display),
                               opts.mode, std::ref(g_stop));

    StreamContext rgb_ctx;
    StreamContext second_ctx;
    rgb_ctx.cfg = make_rgb_config(opts);
    rgb_ctx.render = (opts.mode == "rgb");
    bool const have_second = (opts.mode != "rgb");
    if (have_second) {
        if (opts.mode == "ir")
            second_ctx.cfg = make_ir_config();
        else
            second_ctx.cfg = make_depth_config(opts);
        second_ctx.render = true;
    }

    if (!open_and_prepare(rgb_ctx)) {
        rgb_ctx.capture.close();
        g_stop.store(true);
        preview_hub.set_enabled(false);
        preview_thread.join();
        return 1;
    }
    if (have_second && !open_and_prepare(second_ctx)) {
        rgb_ctx.capture.close();
        second_ctx.capture.close();
        g_stop.store(true);
        preview_hub.set_enabled(false);
        preview_thread.join();
        return 1;
    }

    rgb_ctx.running.store(true);
    if (have_second)
        second_ctx.running.store(true);
    rgb_ctx.worker = std::thread(stream_loop, std::ref(rgb_ctx), std::ref(preview_hub));
    if (have_second)
        second_ctx.worker =
            std::thread(stream_loop, std::ref(second_ctx), std::ref(preview_hub));

    if (rgb_ctx.worker.joinable())
        rgb_ctx.worker.join();
    if (have_second && second_ctx.worker.joinable())
        second_ctx.worker.join();

    g_stop.store(true);
    preview_hub.set_enabled(false);
    preview_thread.join();

    rgb_ctx.capture.close();
    if (have_second)
        second_ctx.capture.close();
    return 0;
}
