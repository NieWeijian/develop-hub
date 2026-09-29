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
 * Host-side RMSL321 UVC live preview on DRM.
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
constexpr int kIrWidth = 1080;
constexpr int kIrHeight = 2560;
constexpr int kTargetFps = 10;
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
    bool require_header = false;
};

struct StreamContext {
    StreamConfig cfg;
    uvc_tools::V4l2Capture capture;
    std::atomic<bool> running{false};
    std::atomic<uint64_t> valid_frames{0};
    std::atomic<uint64_t> header_frames{0};
    std::atomic<uint64_t> invalid_frames{0};
    std::atomic<uint64_t> startup_frames{0};
    std::thread worker;
};

struct AppOptions {
    std::string drm_device = "/dev/dri/by-path/platform-display-subsystem-card";
    int rgb_width = kRgbWidth;
    int rgb_height = kRgbHeight;
    int depth_width = kDepthWidth;
    int depth_height = kDepthHeight;
    int duration_seconds = 0;
    bool ir_mode = false; /* true: capture/display an RGB+IR-only mode */
    bool all_mode = false; /* true: capture/display rgb+depth+ir */
};

class PreviewHub {
public:
    void set_enabled(bool enabled) {
        {
            std::lock_guard<std::mutex> lock(mutex_);
            enabled_ = enabled;
            if (!enabled_) {
                rgb_ = {};
                depth_ = {};
                ir_ = {};
            }
        }
        cv_.notify_all();
    }

    void submit_rgb(uvc_tools::PreviewFrame frame) {
        {
            std::lock_guard<std::mutex> lock(mutex_);
            if (!enabled_)
                return;
            rgb_ = std::move(frame);
            rgb_.valid = true;
        }
        cv_.notify_one();
    }

    void submit_depth(uvc_tools::PreviewFrame frame) {
        {
            std::lock_guard<std::mutex> lock(mutex_);
            if (!enabled_)
                return;
            depth_ = std::move(frame);
            depth_.valid = true;
        }
        cv_.notify_one();
    }

    void submit_ir(uvc_tools::PreviewFrame frame) {
        {
            std::lock_guard<std::mutex> lock(mutex_);
            if (!enabled_)
                return;
            ir_ = std::move(frame);
            ir_.valid = true;
        }
        cv_.notify_one();
    }

    bool wait_latest(uvc_tools::PreviewFrame &rgb,
                     uvc_tools::PreviewFrame &depth,
                     uvc_tools::PreviewFrame &ir,
                     bool need_depth,
                     bool need_ir,
                     std::atomic<bool> const &stop) {
        std::unique_lock<std::mutex> lock(mutex_);
        auto ready = [&] {
            return rgb_.valid && (!need_depth || depth_.valid) && (!need_ir || ir_.valid);
        };
        cv_.wait(lock, [&] { return stop.load() || !enabled_ || ready(); });
        if (stop.load() || !enabled_)
            return false;

        for (;;) {
            rgb = std::move(rgb_);
            rgb_.valid = false;
            if (need_depth) {
                depth = std::move(depth_);
                depth_.valid = false;
            }
            if (need_ir) {
                ir = std::move(ir_);
                ir_.valid = false;
            }
            if (!cv_.wait_for(lock, std::chrono::milliseconds(0), ready))
                break;
        }
        return rgb.valid && (!need_depth || depth.valid) && (!need_ir || ir.valid);
    }

private:
    std::mutex mutex_;
    std::condition_variable cv_;
    uvc_tools::PreviewFrame rgb_;
    uvc_tools::PreviewFrame depth_;
    uvc_tools::PreviewFrame ir_;
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
        if (head->type != ctx.cfg.expected_type) {
            ctx.invalid_frames.fetch_add(1);
            return;
        }
        header_size = head->size;
        ctx.header_frames.fetch_add(1);
    } else if (ctx.cfg.require_header) {
        /* RGB and DEPTH can queue headerless black buffers while the host is
         * still opening the third interface.  Track those separately; once a
         * valid frame arrives, every malformed/headerless frame is an error. */
        if (ctx.valid_frames.load() == 0)
            ctx.startup_frames.fetch_add(1);
        else
            ctx.invalid_frames.fetch_add(1);
        return;
    }
    size_t expected_bytes = 0;
    if (ctx.cfg.format.pixfmt == V4L2_PIX_FMT_NV12)
        expected_bytes = static_cast<size_t>(ctx.capture.width()) * ctx.capture.height() * 3 / 2;
    else if (ctx.cfg.format.pixfmt == V4L2_PIX_FMT_YUYV)
        expected_bytes = static_cast<size_t>(ctx.capture.width()) * ctx.capture.height() * 2;
    if (frame_bytes <= header_size || (expected_bytes > 0 && frame_bytes < expected_bytes)) {
        ctx.invalid_frames.fetch_add(1);
        return;
    }

    uvc_tools::PreviewFrame frame;
    frame.width = ctx.capture.width();
    frame.height = ctx.capture.height();
    auto const *payload = ctx.capture.buffer(buf.index).data() + header_size;
    size_t const payload_size = frame_bytes - header_size;
    if (frame.payload.size() != payload_size)
        frame.payload.resize(payload_size);
    std::memcpy(frame.payload.data(), payload, payload_size);
    frame.valid = true;

    if (ctx.cfg.expected_type == kFrameTypeRgb)
        hub.submit_rgb(std::move(frame));
    else if (ctx.cfg.expected_type == kFrameTypeIr)
        hub.submit_ir(std::move(frame));
    else
        hub.submit_depth(std::move(frame));
    ctx.valid_frames.fetch_add(1);
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
            auto const sibling =
                uvc_tools::sibling_video_node(g_primary_device, ctx.cfg.name_match);
            if (sibling)
                dev_path = *sibling;
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

StreamConfig make_ir_config(bool binned) {
    /* IR is always a combined dual-eye frame; --all uses the binned variant
     * to reduce three-stream USB bandwidth. */
    return StreamConfig{
        "RMSL321_IR",
        "RMSL321_IR",
        {V4L2_PIX_FMT_NV12,
         binned ? kIrWidth / 2 : kIrWidth,
         binned ? kIrHeight / 2 : kIrHeight,
         kTargetFps},
        kFrameTypeIr,
    };
}

void print_usage(char const *prog) {
    std::cout << "Usage: " << prog << " [options]\n"
              << "  --device PATH  DRM card path (default: "
                 "/dev/dri/by-path/platform-display-subsystem-card)\n"
              << "  --rgb WxH      RGB capture resolution (default: 640x540)\n"
              << "  --depth WxH    DEPTH capture resolution (default: 640x352)\n"
              << "  --duration N   Stop cleanly after N seconds (default: run until signal)\n"
              << "  --ir           View rgb+ir instead of rgb+depth\n"
              << "                 For calib use together with --rgb 1080x1280\n"
              << "  --all          View rgb+depth+ir simultaneously\n"
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
        } else if (arg == "--rgb" && i + 1 < argc) {
            if (!parse_resolution(argv[++i], opts.rgb_width, opts.rgb_height)) {
                std::cerr << "Invalid --rgb resolution: " << argv[i] << '\n';
                std::exit(1);
            }
        } else if (arg == "--depth" && i + 1 < argc) {
            if (!parse_resolution(argv[++i], opts.depth_width, opts.depth_height)) {
                std::cerr << "Invalid --depth resolution: " << argv[i] << '\n';
                std::exit(1);
            }
        } else if (arg == "--duration" && i + 1 < argc) {
            try {
                opts.duration_seconds = std::stoi(argv[++i]);
            } catch (...) {
                opts.duration_seconds = 0;
            }
            if (opts.duration_seconds <= 0) {
                std::cerr << "Invalid --duration value: " << argv[i] << '\n';
                std::exit(1);
            }
        } else if (arg == "--ir") {
            opts.ir_mode = true;
        } else if (arg == "--all") {
            opts.all_mode = true;
        } else if (arg == "--help" || arg == "-h") {
            print_usage(argv[0]);
            std::exit(0);
        } else {
            std::cerr << "Unknown option: " << arg << '\n';
            print_usage(argv[0]);
            std::exit(1);
        }
    }
    if (opts.ir_mode && opts.all_mode) {
        std::cerr << "--ir and --all are mutually exclusive\n";
        std::exit(1);
    }
    return opts;
}

void preview_loop(PreviewHub &hub,
                  uvc_tools::DrmPreviewDisplay &display,
                  bool ir_mode,
                  bool all_mode,
                  std::atomic<bool> &stop) {
    uvc_tools::PreviewFrame rgb;
    uvc_tools::PreviewFrame depth;
    uvc_tools::PreviewFrame ir;
    bool logged_present_error = false;
    unsigned long frames = 0;
    auto fps_t0 = std::chrono::steady_clock::now();
    while (!stop.load()) {
        if (!hub.wait_latest(rgb, depth, ir, !ir_mode, ir_mode || all_mode, stop))
            break;
        bool const ok = all_mode   ? display.present_composite_all(rgb, depth, ir)
                        : ir_mode ? display.present_composite_ir(rgb, ir)
                                  : display.present_composite(rgb, depth);
        if (!ok) {
            if (!logged_present_error) {
                std::cerr << "preview: present failed (rgb payload=" << rgb.payload.size()
                          << " depth payload=" << depth.payload.size()
                          << " ir payload=" << ir.payload.size() << ")\n";
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
                               opts.ir_mode, opts.all_mode, std::ref(g_stop));

    StreamContext rgb_ctx;
    StreamContext second_ctx;
    StreamContext ir_ctx;
    rgb_ctx.cfg = make_rgb_config(opts);
    if (opts.ir_mode)
        second_ctx.cfg = make_ir_config(false);
    else
        second_ctx.cfg = make_depth_config(opts);
    if (opts.all_mode)
        ir_ctx.cfg = make_ir_config(true);
    if (opts.all_mode) {
        rgb_ctx.cfg.require_header = true;
        second_ctx.cfg.require_header = true;
        ir_ctx.cfg.require_header = true;
    }

    if (!open_and_prepare(rgb_ctx)) {
        rgb_ctx.capture.close();
        g_stop.store(true);
        preview_hub.set_enabled(false);
        preview_thread.join();
        return 1;
    }
    if (!open_and_prepare(second_ctx)) {
        rgb_ctx.capture.close();
        second_ctx.capture.close();
        g_stop.store(true);
        preview_hub.set_enabled(false);
        preview_thread.join();
        return 1;
    }
    if (opts.all_mode && !open_and_prepare(ir_ctx)) {
        rgb_ctx.capture.close();
        second_ctx.capture.close();
        ir_ctx.capture.close();
        g_stop.store(true);
        preview_hub.set_enabled(false);
        preview_thread.join();
        return 1;
    }

    rgb_ctx.running.store(true);
    second_ctx.running.store(true);
    ir_ctx.running.store(opts.all_mode);
    rgb_ctx.worker = std::thread(stream_loop, std::ref(rgb_ctx), std::ref(preview_hub));
    second_ctx.worker =
        std::thread(stream_loop, std::ref(second_ctx), std::ref(preview_hub));
    if (opts.all_mode)
        ir_ctx.worker = std::thread(stream_loop, std::ref(ir_ctx), std::ref(preview_hub));

    std::thread duration_thread;
    if (opts.duration_seconds > 0) {
        duration_thread = std::thread([seconds = opts.duration_seconds] {
            const auto deadline = Clock::now() + std::chrono::seconds(seconds);
            while (!g_stop.load() && Clock::now() < deadline)
                std::this_thread::sleep_for(std::chrono::milliseconds(100));
            g_stop.store(true);
        });
    }

    if (rgb_ctx.worker.joinable())
        rgb_ctx.worker.join();
    if (second_ctx.worker.joinable())
        second_ctx.worker.join();
    if (ir_ctx.worker.joinable())
        ir_ctx.worker.join();
    if (duration_thread.joinable())
        duration_thread.join();

    g_stop.store(true);
    preview_hub.set_enabled(false);
    preview_thread.join();

    bool all_streams_valid = true;
    if (opts.all_mode) {
        for (StreamContext *ctx : {&rgb_ctx, &second_ctx, &ir_ctx}) {
            const uint64_t frames = ctx->valid_frames.load();
            const uint64_t headers = ctx->header_frames.load();
            const uint64_t invalid = ctx->invalid_frames.load();
            const uint64_t startup = ctx->startup_frames.load();
            std::cout << "[summary] " << ctx->cfg.label << " valid=" << frames
                      << " headers=" << headers << " invalid=" << invalid
                      << " startup=" << startup << '\n';
            if (frames == 0 || headers != frames || invalid != 0)
                all_streams_valid = false;
        }
    }

    rgb_ctx.capture.close();
    second_ctx.capture.close();
    ir_ctx.capture.close();
    return all_streams_valid ? 0 : 2;
}
