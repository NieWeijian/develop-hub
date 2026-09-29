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
 * ISP 3A test: open RGB + IR streams and exercise get/set of the standard PU
 * controls on both. Frame rate is NOT a pass criterion (IR is ~2fps); the
 * streams only need to deliver frames and every 3A/PU control must set and
 * read back. In normal rgb+depth mode there is no IR UVC node, so the IR
 * lane rides the DEPTH node instead — the depth camera is the IR sensor pair
 * and its 3A controls are reachable the same way.
 *
 * Build (from tools/):
 *   cmake -B build && cmake --build build
 *   ./build/uvc_host_isp_test [rounds]
 */

#include "uvc_device.hpp"
#include "v4l2_capture.hpp"

#include <algorithm>
#include <array>
#include <atomic>
#include <charconv>
#include <chrono>
#include <cstdint>
#include <iostream>
#include <linux/v4l2-controls.h>
#include <linux/videodev2.h>
#include <mutex>
#include <string>
#include <string_view>
#include <sys/ioctl.h>
#include <system_error>
#include <thread>
#include <vector>

namespace {

enum class Lane : unsigned { Rgb, Ir };

constexpr unsigned k_rgb_ir_buffers = 12;
constexpr unsigned k_warmup_frames = 8;
constexpr unsigned k_default_rounds = 10;
constexpr auto k_ctrl_settle = std::chrono::microseconds(50'000);
constexpr auto k_poll_timeout = std::chrono::milliseconds(500);

constexpr std::array<uint32_t, 3> k_pix_fallback{
    V4L2_PIX_FMT_NV12, V4L2_PIX_FMT_YUYV, V4L2_PIX_FMT_MJPEG};

constexpr std::string_view lane_name(Lane lane) {
    switch (lane) {
    case Lane::Rgb: return "rgb";
    case Lane::Ir: return "ir";
    }
    return "?";
}

unsigned lane_index(Lane lane) {
    return static_cast<unsigned>(lane);
}

struct CaptureProfile {
    std::string_view card;
    uvc_tools::CaptureFormat format;
};

constexpr CaptureProfile k_rgb{"RMSL321_RGB", {V4L2_PIX_FMT_NV12, 640, 540, 10}};
constexpr CaptureProfile k_ir{"RMSL321_IR", {V4L2_PIX_FMT_NV12, 1080, 2560, 10}};

class Logger {
    mutable std::mutex mtx_;

public:
    void info(Lane lane, std::string_view msg) const {
        std::lock_guard lock(mtx_);
        std::cerr << "[cam" << lane_index(lane) << "] " << msg;
        if (msg.empty() || msg.back() != '\n')
            std::cerr << '\n';
    }

    void error(Lane lane, std::string_view op, std::error_code ec) const {
        info(lane, std::string(op) + ": " + ec.message() + "\n");
    }
};

Logger g_log;

std::error_code v4l2_ec() {
    return {errno, std::generic_category()};
}

class RoundStats {
    unsigned ok_ = 0;
    unsigned fail_ = 0;

public:
    void add(bool pass) {
        pass ? ++ok_ : ++fail_;
    }
    bool passed() const {
        return fail_ == 0;
    }
    unsigned ok() const {
        return ok_;
    }
    unsigned fail() const {
        return fail_;
    }
};

struct IspStep {
    uint32_t id;
    int32_t val;
    std::string_view name;
    bool settle_after;
};

constexpr std::array<IspStep, 15> k_isp_steps{{
    {V4L2_CID_HUE_AUTO, 0, "hue_auto_off", false},
    {V4L2_CID_BRIGHTNESS, 60, "brightness", false},
    {V4L2_CID_CONTRAST, 55, "contrast", false},
    {V4L2_CID_HUE, 50, "hue", false},
    {V4L2_CID_SATURATION, 50, "saturation", false},
    {V4L2_CID_SHARPNESS, 50, "sharpness", false},
    {V4L2_CID_GAMMA, 100, "gamma", false},
    {V4L2_CID_WHITE_BALANCE_TEMPERATURE, 5000, "wb_temp", false},
    {V4L2_CID_EXPOSURE_AUTO, 0, "ae_sync_auto", true},
    {V4L2_CID_EXPOSURE_AUTO, 1, "ae_manual", true},
    {V4L2_CID_GAIN, 150, "gain", true},
    {V4L2_CID_EXPOSURE_ABSOLUTE, 100, "exp_100us", true},
    {V4L2_CID_EXPOSURE_AUTO, 0, "ae_restore_auto", false},
    {V4L2_CID_HUE_AUTO, 1, "hue_auto", false},
    {V4L2_CID_POWER_LINE_FREQUENCY, V4L2_CID_POWER_LINE_FREQUENCY_50HZ, "power_line", false},
}};

class V4l2Isp {
public:
    explicit V4l2Isp(Lane lane, int fd) : lane_(lane), fd_(fd) {}

    bool set_control(uint32_t id, int32_t val, std::string_view name, RoundStats &stats) {
        v4l2_queryctrl qc{};
        qc.id = id;
        if (ioctl(fd_, VIDIOC_QUERYCTRL, &qc) != 0) {
            g_log.info(lane_, "  FAIL " + std::string(name) + " (not available)\n");
            stats.add(false);
            return false;
        }
        if (qc.flags & V4L2_CTRL_FLAG_DISABLED) {
            g_log.info(lane_, "  FAIL " + std::string(name) + " (disabled)\n");
            stats.add(false);
            return false;
        }
        val = std::clamp(val, qc.minimum, qc.maximum);
        v4l2_control ctrl{id, val};
        if (ioctl(fd_, VIDIOC_S_CTRL, &ctrl) != 0) {
            g_log.error(lane_, std::string(name), v4l2_ec());
            stats.add(false);
            return false;
        }
        stats.add(true);
        return true;
    }

    bool get_control(uint32_t id, std::string_view name, RoundStats &stats) {
        v4l2_control ctrl{id, 0};
        if (ioctl(fd_, VIDIOC_G_CTRL, &ctrl) != 0) {
            g_log.error(lane_, std::string(name) + " get", v4l2_ec());
            stats.add(false);
            return false;
        }
        stats.add(true);
        return true;
    }

    bool has_standard_pu() const {
        v4l2_queryctrl qc{};
        qc.id = V4L2_CID_CONTRAST;
        return ioctl(fd_, VIDIOC_QUERYCTRL, &qc) == 0 && !(qc.flags & V4L2_CTRL_FLAG_DISABLED);
    }

private:
    Lane lane_;
    int fd_;
};

class StreamSession {
public:
    StreamSession(Lane lane, std::string device, CaptureProfile prof, unsigned buffer_count)
        : lane_(lane),
          device_(std::move(device)),
          profile_(prof),
          buffer_count_(buffer_count) {}

    bool start() {
        if (!capture_.open(device_))
            goto fail;

        // IR: try the combined 1080x2560 / 540x1280 NV12 first; on the DEPTH
        // node (normal rgb+depth mode) fall back to the largest YUYV size.
        if (lane_ == Lane::Ir) {
            bool ok = capture_.configure(
                          {V4L2_PIX_FMT_NV12, 1080, 2560, profile_.format.fps}, buffer_count_) ||
                      capture_.configure(
                          {V4L2_PIX_FMT_NV12, 540, 1280, profile_.format.fps}, buffer_count_);
            if (!ok) {
                auto sizes = capture_.enumerate_frame_sizes(V4L2_PIX_FMT_YUYV);
                std::sort(sizes.begin(), sizes.end(), [](auto const &a, auto const &b) {
                    return a.first * a.second > b.first * b.second;
                });
                for (auto const &s : sizes) {
                    if (capture_.configure({V4L2_PIX_FMT_YUYV, s.first, s.second,
                                            profile_.format.fps},
                                           buffer_count_)) {
                        ok = true;
                        break;
                    }
                }
            }
            if (!ok)
                goto fail;
        } else {
            auto fmt = profile_.format;
            const auto sizes = capture_.enumerate_frame_sizes(V4L2_PIX_FMT_NV12);
            const auto requested = std::make_pair(fmt.width, fmt.height);
            const auto calib = std::make_pair(1080, 1280);
            if (std::find(sizes.begin(), sizes.end(), requested) == sizes.end() &&
                std::find(sizes.begin(), sizes.end(), calib) != sizes.end()) {
                fmt.width = calib.first;
                fmt.height = calib.second;
            }
            if (!capture_.configure_with_fallback(
                    fmt, {k_pix_fallback.begin(), k_pix_fallback.end()}, buffer_count_)) {
                goto fail;
            }
        }

        if (!capture_.start())
            goto fail;

        g_log.info(lane_, "VIDIOC_STREAMON\n");
        frame_count_.store(0);
        worker_error_.store(false);
        stop_.store(false);
        worker_ = std::thread([this] { capture_loop(); });
        return true;

    fail:
        capture_.close();
        return false;
    }

    void stop_worker() {
        stop_.store(true);
        if (worker_.joinable())
            worker_.join();
    }

    void stream_off() {
        stop_worker();
        g_log.info(lane_, "VIDIOC_STREAMOFF\n");
        capture_.close();
    }

    bool worker_ok() const {
        return !worker_error_.load();
    }

    bool wait_frames(unsigned count, std::chrono::milliseconds timeout) const {
        if (count == 0)
            return true;
        unsigned const start = frame_count_.load(std::memory_order_relaxed);
        auto const deadline = std::chrono::steady_clock::now() + timeout;
        while (frame_count_.load(std::memory_order_relaxed) - start < count) {
            if (worker_error_.load())
                return false;
            if (std::chrono::steady_clock::now() >= deadline) {
                g_log.info(lane_,
                           "timeout waiting " + std::to_string(count) + " frames (got " +
                               std::to_string(frame_count_.load() - start) + ")\n");
                return false;
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
        }
        return true;
    }

    bool run_round(unsigned round, unsigned total_rounds) {
        RoundStats stats;
        g_log.info(lane_,
                   std::string("[") + std::string(lane_name(lane_)) + "] round " +
                       std::to_string(round + 1) + "/" + std::to_string(total_rounds) + "\n");

        if (round == 0) {
            std::lock_guard lock(io_mtx_);
            if (!V4l2Isp(lane_, capture_.fd()).has_standard_pu())
                g_log.info(lane_, "  WARN: limited PU on host node\n");
        }

        V4l2Isp isp(lane_, capture_.fd());
        for (IspStep const &step : k_isp_steps) {
            {
                std::lock_guard lock(io_mtx_);
                isp.set_control(step.id, step.val, step.name, stats);
            }
            {
                std::lock_guard lock(io_mtx_);
                isp.get_control(step.id, step.name, stats);
            }
            if (step.settle_after)
                std::this_thread::sleep_for(k_ctrl_settle);
        }

        g_log.info(lane_,
                   std::string("[") + std::string(lane_name(lane_)) + "] round " +
                       std::to_string(round + 1) + "/" + std::to_string(total_rounds) + ": " +
                       (stats.passed() ? "SUCCESS" : "FAIL") + " (ok=" + std::to_string(stats.ok()) +
                       " fail=" + std::to_string(stats.fail()) + ")\n");
        return stats.passed();
    }

    Lane lane() const {
        return lane_;
    }

private:
    void capture_loop() {
        while (!stop_.load()) {
            if (!capture_.poll_readable(k_poll_timeout))
                continue;

            v4l2_buffer buf{};
            {
                std::lock_guard lock(io_mtx_);
                try {
                    if (!capture_.try_dequeue(buf)) {
                        if (errno == EAGAIN)
                            continue;
                        if (errno != ENODEV && !stop_.load())
                            worker_error_.store(true);
                        return;
                    }
                } catch (std::system_error const &) {
                    if (!stop_.load())
                        worker_error_.store(true);
                    return;
                }
            }

            frame_count_.fetch_add(1, std::memory_order_relaxed);

            {
                std::lock_guard lock(io_mtx_);
                try {
                    capture_.queue(buf);
                } catch (std::system_error const &) {
                    if (!stop_.load())
                        worker_error_.store(true);
                    return;
                }
            }
        }
    }

    Lane lane_;
    std::string device_;
    CaptureProfile profile_;
    unsigned buffer_count_;
    uvc_tools::V4l2Capture capture_;
    std::atomic<bool> stop_{false};
    std::atomic<unsigned> frame_count_{0};
    std::atomic<bool> worker_error_{false};
    std::thread worker_;
    mutable std::mutex io_mtx_;
};

class IspStressTest {
public:
    explicit IspStressTest(unsigned rounds, std::string const &device = {})
        : rounds_(rounds), device_(device) {}

    int run() {
        std::string rgb_dev;
        std::string ir_dev;
        if (!device_.empty()) {
            rgb_dev = device_;
            if (auto ir = uvc_tools::sibling_video_node(device_, "RMSL321_IR"))
                ir_dev = *ir;
            else if (auto dp = uvc_tools::sibling_video_node(device_, "RMSL321_DEPTH"))
                ir_dev = *dp; /* normal rgb+depth mode: no IR UVC, use DEPTH */
        } else {
            auto const rgb = uvc_tools::find_capture_device_any({"RMSL321_RGB", "RMSL322_RGB"});
            auto const ir = uvc_tools::find_capture_device_any({"RMSL321_IR", "RMSL322_IR"});
            auto const dp = uvc_tools::find_capture_device_any({"RMSL321_DEPTH", "RMSL322_DEPTH"});
            if (rgb)
                rgb_dev = *rgb;
            if (ir)
                ir_dev = *ir;
            else if (dp)
                ir_dev = *dp; /* normal rgb+depth mode: use DEPTH as IR lane */
        }
        if (rgb_dev.empty() || ir_dev.empty()) {
            std::cerr << "need RGB+IR (found rgb=" << !rgb_dev.empty()
                      << " ir=" << !ir_dev.empty()
                      << ")\n";
            return 1;
        }

        std::cerr << "ISP stress test: stream on -> " << rounds_
                  << " round(s) rgb/ir 3A get/set -> streams off\n"
                  << "  RGB=" << rgb_dev << " IR=" << ir_dev
                  << " warmup=" << k_warmup_frames << " frames\n";

        StreamSession rgb(Lane::Rgb, rgb_dev, k_rgb, k_rgb_ir_buffers);
        StreamSession ir(Lane::Ir, ir_dev, k_ir, k_rgb_ir_buffers);

        if (!rgb.start() || !ir.start()) {
            shutdown(rgb, ir);
            return 1;
        }

        /* IR streams at ~2 fps, so 8 warmup frames can take ~4s plus startup
         * latency; 20s gives a generous fixed window. Frame rate is not a
         * pass criterion — only that both streams deliver frames. */
        auto warmup_timeout = std::chrono::seconds(20);
        bool ready = rgb.wait_frames(k_warmup_frames, warmup_timeout) &&
                     ir.wait_frames(k_warmup_frames, warmup_timeout) &&
                     rgb.worker_ok() && ir.worker_ok();

        bool rgb_pass = false;
        bool ir_pass = false;
        int rc = 0;

        if (!ready) {
            rc = 1;
        } else {
            rgb_pass = true;
            ir_pass = true;
            for (unsigned round = 0; round < rounds_; ++round) {
                if (!rgb.worker_ok() || !ir.worker_ok()) {
                    g_log.info(Lane::Rgb, "stream worker exited before round " +
                                              std::to_string(round + 1) + "\n");
                    rgb_pass = false;
                    ir_pass = false;
                    rc = 1;
                    break;
                }
                if (!rgb.run_round(round, rounds_))
                    rgb_pass = false;
                if (!rgb.worker_ok() || !ir.worker_ok()) {
                    rgb_pass = false;
                    ir_pass = false;
                    rc = 1;
                    break;
                }
                if (!ir.run_round(round, rounds_))
                    ir_pass = false;
                if (!rgb.worker_ok() || !ir.worker_ok())
                    rc = 1;
                if (round + 1 < rounds_)
                    std::this_thread::sleep_for(k_ctrl_settle);
            }

        }

        shutdown(rgb, ir);

        bool const ok = rgb_pass && ir_pass;
        std::cerr << "summary: rgb=" << (rgb_pass ? "SUCCESS" : "FAIL")
                  << " ir=" << (ir_pass ? "SUCCESS" : "FAIL")
                  << " => " << (ok ? "SUCCESS" : "FAIL") << '\n';
        return rc != 0 ? 1 : (ok ? 0 : 1);
    }

private:
    void shutdown(StreamSession &rgb, StreamSession &ir) {
        ir.stream_off();
        rgb.stream_off();
    }

    unsigned rounds_;
    std::string device_;
};

unsigned parse_uint(std::string_view text, unsigned default_val, unsigned max_val) {
    if (text.empty())
        return default_val;
    unsigned value = 0;
    auto const result = std::from_chars(text.data(), text.data() + text.size(), value);
    if (result.ec != std::errc{} || value == 0 || value > max_val)
        return default_val;
    return value;
}

} // namespace

int main(int argc, char **argv) {
    unsigned const rounds =
        parse_uint(argc > 1 ? std::string_view(argv[1]) : std::string_view{}, k_default_rounds, 1000);
    std::string device;
    for (int i = 1; i < argc - 1; ++i) {
        if (std::string(argv[i]) == "--device")
            device = argv[i + 1];
    }
    return IspStressTest(rounds, device).run();
}
