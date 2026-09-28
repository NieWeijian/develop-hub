/*
 * Copyright (C) 2026 Rockchip Electronics Co., Ltd.
 *
 * RMSL321 host-side factory test tool.
 *
 * Phases (no overlap with isp_test):
 *   Phase 1 — Device detection + USB link health  (VID/PID/SN/speed/power)
 *   Phase 2 — Stream quality + IR dual-eye check  (fps, drops, left/right valid)
 *   Phase 3 — Module telemetry via HID              (temp/I2C/MIPI/clocks)
 *   Phase 4 — Burn-in / stress                    (long run with periodic checks)
 *
 * Build: cmake -B build && cmake --build build
 *
 * Usage:
 *   ./uvc_host_ft                    # standard test
 *   ./uvc_host_ft --quick            # quick smoke
 *   ./uvc_host_ft --long             # extended test + burn-in
 *   ./uvc_host_ft --json             # JSON output
 */

#include "v4l2_capture.hpp"
#include "ctl_proto.h"
#include "ctl_xu.hpp"

#include <algorithm>
#include <array>
#include <atomic>
#include <charconv>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <linux/usb/video.h>
#include <linux/uvcvideo.h>
#include <linux/videodev2.h>
#include <mutex>
#include <optional>
#include <sstream>
#include <string>
#include <string_view>
#include <sys/ioctl.h>
#include <system_error>
#include <thread>
#include <vector>

using Clock = std::chrono::steady_clock;
using TimePoint = Clock::time_point;

namespace {

/* =========================================================================
 * Constants
 * ========================================================================= */

constexpr int kTargetFps = 10;
constexpr int kBufferCount = 6;

constexpr auto kPollTimeout = std::chrono::milliseconds(500);

/* Test result types */
enum class TestResult : int { PASS = 0, FAIL = 1, SKIP = 2 };

char const *result_str(TestResult r) {
    switch (r) {
    case TestResult::PASS: return "PASS";
    case TestResult::FAIL: return "FAIL";
    case TestResult::SKIP: return "SKIP";
    }
    return "?";
}

struct TestPhase { std::string name; TestResult result = TestResult::SKIP; std::string detail{}; double duration_sec = 0; };
struct OverallReport { TestResult overall = TestResult::PASS; std::vector<TestPhase> phases; std::string timestamp, hostname; };

/* Helpers */
std::mutex g_print_mtx;
void log_info(std::string_view msg) { std::lock_guard lk(g_print_mtx); std::cerr << "[FT] " << msg << (msg.empty()||msg.back()!='\n'?"\n":""); }
void log_warn(std::string_view msg) { std::lock_guard lk(g_print_mtx); std::cerr << "[FT] WARN: " << msg << (msg.empty()||msg.back()!='\n'?"\n":""); }

std::string now_str() { auto t = std::chrono::system_clock::to_time_t(std::chrono::system_clock::now()); std::ostringstream oss; oss << std::put_time(std::localtime(&t), "%Y-%m-%d %H:%M:%S"); return oss.str(); }
std::string duration_str(double sec) { std::ostringstream oss; if (sec < 60.0) oss << std::fixed << std::setprecision(1) << sec << "s"; else if (sec < 3600.0) oss << sec/60.0 << "min"; else oss << sec/3600.0 << "h"; return oss.str(); }

static std::string read_sysfs_str(char const *path) {
    std::ifstream f(path); if (!f) return {};
    std::string s; std::getline(f, s);
    while (!s.empty() && (s.back()=='\n'||s.back()=='\r')) s.pop_back();
    return s;
}

/* =========================================================================
 * Control access (UVC XU)
 * ========================================================================= */

static ctl_xu::Link g_ctl;

static std::string ctl_text(uint32_t cmd) {
    return ctl_xu::text(&g_ctl, cmd);
}

static int text_field_int(const std::string &text, const std::string &key, int def) {
    const std::string marker = key + "=";
    const size_t p = text.find(marker);
    if (p == std::string::npos)
        return def;
    int v = def;
    const auto [ptr, ec] =
        std::from_chars(text.data() + p + marker.size(), text.data() + text.size(), v);
    return ec == std::errc() ? v : def;
}

/* =========================================================================
 * Run options
 * ========================================================================= */
struct RunOptions {
    bool quick = false, long_mode = false, json_out = false;
    unsigned stream_sec = 30, burn_in_sec = 3600;
    std::string device;
};

/* Pinned module (--device): RGB uses it, DEPTH/IR derived from the same USB
 * device. Empty keeps the legacy auto-find. */
std::string g_primary_device;

std::string node_for(std::string const &suffix) {
    if (!g_primary_device.empty()) {
        if (suffix == "RGB")
            return g_primary_device;
        if (auto s = uvc_tools::sibling_video_node(g_primary_device, "RMSL321_" + suffix))
            return *s;
        return {};
    }
    auto dev = uvc_tools::find_capture_device_any(
        {"RMSL321_" + suffix, "RMSL322_" + suffix});
    return dev ? *dev : std::string{};
}

RunOptions parse_opts(int argc, char **argv) {
    RunOptions o;
    for (int i = 1; i < argc; ++i) {
        std::string_view a = argv[i];
        if (a == "--quick") { o.quick = true; o.stream_sec = 15; }
        else if (a == "--long") { o.long_mode = true; o.stream_sec = 300; }
        else if (a == "--json") o.json_out = true;
        else if ((a == "--device" || a == "--video") && i + 1 < argc) o.device = argv[++i];
        else if ((a == "--stream-sec" || a == "--burn-in-sec") && i + 1 < argc) {
            unsigned v = 0;
            auto r = std::from_chars(argv[i+1], argv[i+1]+strlen(argv[i+1]), v);
            if (r.ec == std::errc{}) { if (a == "--stream-sec") o.stream_sec = v; else o.burn_in_sec = v; ++i; }
        } else if (a == "--help" || a == "-h") {
            std::cout << "Usage: uvc_host_ft [--quick|--long|--json|--stream-sec N|--burn-in-sec N]\n";
            std::exit(0);
        }
    }
    return o;
}

/* =========================================================================
 * Phase 1: Device detection + USB link health
 * ========================================================================= */

TestPhase phase_device_and_usb() {
    TestPhase ph{"Device + USB Link", TestResult::SKIP};
    auto t0 = Clock::now();
    std::ostringstream d;

    /* UVC device detection */
    struct { char const *label; const char *suffix; } lanes[] = {{"RGB","RGB"},{"DEPTH","DEPTH"},{"IR","IR"}};
    int found = 0;
    std::string first_dev;
    for (auto &l : lanes) {
        std::string dev = node_for(l.suffix);
        d << "  " << l.label << ": " << (dev.empty() ? "MISSING" : dev) << "\n";
        if (!dev.empty()) { ++found; if (first_dev.empty()) first_dev = dev; }
    }
    d << "  Found: " << found << "/3\n";

    /* USB link info from sysfs — walk from video node to USB device */
    std::string vid, pid, sn, speed, power;
    if (!first_dev.empty()) {
        // Extract videoN from /dev/videoN
        std::string vname = first_dev.substr(5); // "video25"
        std::string sysbase = "/sys/class/video4linux/" + vname + "/device";

        // Walk up to find USB device info
        auto read_sys = [](std::string const &p) { return read_sysfs_str(p.c_str()); };
        vid = read_sys(sysbase + "/../idVendor");
        pid = read_sys(sysbase + "/../idProduct");
        sn  = read_sys(sysbase + "/../serial");
        speed = read_sys(sysbase + "/../speed");
        power = read_sys(sysbase + "/../bMaxPower");

        // speed value: 5000 = SuperSpeed (5Gbps), 480 = High-Speed, 12 = Full-Speed
        std::string speed_str = "unknown";
        if (!speed.empty()) {
            int sp = std::stoi(speed);
            if (sp >= 5000) speed_str = "SuperSpeed 5Gbps";
            else if (sp >= 480) speed_str = "High-Speed 480Mbps";
            else speed_str = speed + " Mbps";
        }
        d << "\n  USB: " << vid << ":" << pid << "  SN: " << sn << "\n";
        d << "  Link: " << speed_str << "  Power: " << power << "\n";
    }

    ph.result = (found >= 2) ? TestResult::PASS : TestResult::FAIL;
    ph.detail = d.str();
    ph.duration_sec = std::chrono::duration<double>(Clock::now() - t0).count();
    return ph;
}

/* =========================================================================
 * Phase 2: Stream quality + IR dual-eye check
 * ========================================================================= */

struct StreamStats { std::atomic<uint64_t> frames{0}, seq_drops{0}; std::atomic<int32_t> last_seq{-1}; std::atomic<bool> worker_ok{true}; double final_fps = 0; uint64_t final_drops = 0; };

struct StreamProfile { char const *label; const char *suffix; uvc_tools::CaptureFormat fmt; uint16_t expect_type; bool check_dual; };

constexpr StreamProfile kProfiles[] = {
    {"RGB",   "RGB",   {V4L2_PIX_FMT_NV12, 640, 540, 10}, 1, false},
    {"DEPTH", "DEPTH", {V4L2_PIX_FMT_YUYV, 640, 352, 10}, 2, false},
    {"IR",    "IR",    {V4L2_PIX_FMT_NV12, 1080, 2560, 10}, 0, true},
};

#pragma pack(push, 1)
/* Matches UvcHeadV2 in src/app/struct_light_output.cpp (module side). */
struct UvcHeadV2 { uint16_t magic, version, size, type, width, height; int32_t sequence; int64_t timestamp; float temperature; uint32_t exposure_us, readout_time_us; };
#pragma pack(pop)

struct StreamSession { StreamProfile const *prof = nullptr; uvc_tools::V4l2Capture cap; StreamStats st; std::atomic<bool> stop{false}, ir_left_ok{false}, ir_right_ok{false}; std::thread worker; };

static void stream_worker(StreamSession *s) {
    while (!s->stop.load()) {
        if (!s->cap.poll_readable(kPollTimeout)) continue;
        while (!s->stop.load()) {
            v4l2_buffer buf{};
            try { if (!s->cap.try_dequeue(buf)) break; } catch (...) { s->st.worker_ok.store(false); return; }
            s->st.frames.fetch_add(1);

            size_t bytes = buf.bytesused > 0 ? buf.bytesused : buf.length;
            if (s->prof->expect_type) {
                UvcHeadV2 h{};
                if (bytes >= sizeof(h)) { memcpy(&h, s->cap.buffer(buf.index).data(), sizeof(h));
                    if (h.magic == 0xA55A && h.version == 0x02 && h.type == s->prof->expect_type) {
                        int32_t last = s->st.last_seq.load();
                        if (last >= 0 && h.sequence > last + 1) s->st.seq_drops.fetch_add(h.sequence - last - 1);
                        s->st.last_seq.store(h.sequence);
                    }
                }
            }

            /* IR dual-eye: sample a few pixels from top (left) and bottom (right) halves */
            if (s->prof->check_dual && bytes >= size_t(s->cap.width()) * s->cap.height() * 3 / 2) {
                int w = s->cap.width(), h = s->cap.height(), half_h = h / 2;
                auto const *p = reinterpret_cast<uint8_t const *>(s->cap.buffer(buf.index).data());
                // sample 4 points from left half (rows 100, half_h-1) and right half (rows half_h+100, h-1)
                bool left_ok = (p[100 * w + w/4] != 0 || p[(half_h - 1) * w + w/4] != 0);
                bool right_ok = (p[(half_h + 100) * w + w/4] != 0 || p[(h - 1) * w + w/4] != 0);
                s->ir_left_ok.store(s->ir_left_ok.load() || left_ok);
                s->ir_right_ok.store(s->ir_right_ok.load() || right_ok);
            }

            try { s->cap.queue(buf); } catch (...) { s->st.worker_ok.store(false); return; }
        }
    }
}

static bool start_stream(StreamSession *s) {
    std::string dev = node_for(s->prof->suffix);
    if (dev.empty()) return false;
    if (!s->cap.open(dev)) return false;
    if (s->prof->check_dual) {
        if (!s->cap.configure({V4L2_PIX_FMT_NV12, 1080, 2560, 10}, 12) &&
            !s->cap.configure({V4L2_PIX_FMT_NV12, 540, 1280, 10}, 12)) { s->cap.close(); return false; }
    } else {
        std::vector<uint32_t> fb{V4L2_PIX_FMT_NV12, V4L2_PIX_FMT_YUYV, V4L2_PIX_FMT_MJPEG};
        if (!s->cap.configure_with_fallback(s->prof->fmt, fb, kBufferCount)) { s->cap.close(); return false; }
    }
    if (!s->cap.start()) { s->cap.close(); return false; }
    s->worker = std::thread(stream_worker, s);
    return true;
}

static void stop_stream(StreamSession *s) { s->stop.store(true); if (s->worker.joinable()) s->worker.join(); s->cap.close(); }

TestPhase phase_stream_test(unsigned dur_sec) {
    TestPhase ph{"Stream + IR Dual-Eye", TestResult::SKIP};
    auto t0 = Clock::now();
    std::ostringstream d;

    /* No fps/drops statistics in FT: opening IR impacts the frame rate, and
     * frame rate is covered by uvc_host_stream (RGB+DEPTH, real scenario).
     * Here we only verify that all three streams deliver frames and that the
     * IR dual-eye (calibration-only) has valid left/right halves. */
    (void)dur_sec;

    /* Mode-aware: only stream the UVCs present in the current device mode
     * (rgb+depth / rgb+ir); absent streams are SKIPped, not failed. */
    StreamSession ss[3] = {};
    bool avail[3] = {};
    for (int i = 0; i < 3; ++i) {
        ss[i].prof = &kProfiles[i];
        avail[i] = !node_for(kProfiles[i].suffix).empty();
        if (!avail[i])
            d << "  " << kProfiles[i].label << ": SKIP (not in current mode)\n";
    }

    int started = 0;
    for (int i = 0; i < 3; ++i) {
        if (!avail[i])
            continue;
        if (start_stream(&ss[i])) {
            ++started;
            d << "  " << ss[i].prof->label << ": " << ss[i].cap.width() << "x"
              << ss[i].cap.height() << "@" << ss[i].prof->fmt.fps << "fps\n";
        } else {
            d << "  " << ss[i].prof->label << ": FAILED\n";
        }
    }

    const int expect = (avail[0] ? 1 : 0) + (avail[1] ? 1 : 0) + (avail[2] ? 1 : 0);
    if (expect < 2 || started != expect) {
        for (int i = 0; i < 3; ++i)
            stop_stream(&ss[i]);
        ph.result = TestResult::FAIL;
        ph.detail = d.str();
        ph.duration_sec = std::chrono::duration<double>(Clock::now() - t0).count();
        return ph;
    }

    /* Sample briefly: wait until every started stream delivered a frame and IR
     * dual-eye saw both halves, with a hard cap. */
    bool all_pass = true;
    auto sample_deadline = Clock::now() + std::chrono::seconds(5);
    while (Clock::now() < sample_deadline) {
        bool all_frames = true;
        for (int i = 0; i < 3; ++i)
            if (avail[i] && ss[i].st.frames.load() == 0)
                all_frames = false;
        const bool dual_ok = !avail[2] ||
                             (ss[2].ir_left_ok.load() && ss[2].ir_right_ok.load());
        if (all_frames && dual_ok)
            break;
        std::this_thread::sleep_for(std::chrono::milliseconds(200));
    }

    d << "\n  Results (no fps/drops statistics, see uvc_host_stream):\n";
    for (int i = 0; i < 3; ++i) {
        stop_stream(&ss[i]);
        if (!avail[i]) {
            d << "    " << ss[i].prof->label << ": SKIP\n";
            continue;
        }
        uint64_t frames = ss[i].st.frames.load();
        bool ok = frames > 0;
        d << "    " << ss[i].prof->label << ": frames=" << frames << " => " << (ok ? "PASS" : "FAIL") << "\n";
        if (!ok) all_pass = false;
    }
    if (avail[2]) {
        bool lo = ss[2].ir_left_ok.load(), ro = ss[2].ir_right_ok.load();
        d << "    IR dual-eye: L=" << (lo ? "OK" : "BLANK") << " R=" << (ro ? "OK" : "BLANK")
          << " => " << ((lo && ro) ? "PASS" : "FAIL") << "\n";
        if (!lo || !ro)
            all_pass = false;
    } else {
        d << "    IR dual-eye: SKIP (IR not in current mode)\n";
    }

    ph.result = all_pass ? TestResult::PASS : TestResult::FAIL;
    ph.detail = d.str();
    ph.duration_sec = std::chrono::duration<double>(Clock::now()-t0).count();
    return ph;
}

/* =========================================================================
 * Phase 3: Module telemetry via HID (ft_status)
 * ========================================================================= */

TestPhase phase_module_telemetry() {
    TestPhase ph{"Module Telemetry (HID)", TestResult::SKIP};
    auto t0 = Clock::now();
    std::ostringstream d;

    if (g_ctl.fd < 0) {
        d << "  control channel: UNAVAILABLE (XU open failed)\n";
        ph.result = TestResult::FAIL;
        ph.detail = d.str();
        ph.duration_sec = std::chrono::duration<double>(Clock::now() - t0).count();
        return ph;
    }
    const std::string status = ctl_text(rmsl_ctl::kCmdGetFtStatus);
    if (status.empty()) {
        d << "  ft_status: UNAVAILABLE (HID query failed)\n";
        ph.result = TestResult::FAIL; ph.detail = d.str(); ph.duration_sec = std::chrono::duration<double>(Clock::now()-t0).count(); return ph;
    }

    d << "  control node: " << g_ctl.video << "\n";
    d << "  ft_status:    " << status << "\n";
    const std::string ddr = ctl_text(rmsl_ctl::kCmdGetFtDdr);
    if (!ddr.empty())
        d << "  ft_ddr:       " << ddr << "\n";
    const std::string flash = ctl_text(rmsl_ctl::kCmdGetFtFlash);
    if (!flash.empty())
        d << "  ft_flash:     " << flash << "\n";
    const std::string mipi = ctl_text(rmsl_ctl::kCmdGetFtMipi);
    if (!mipi.empty())
        d << "  ft_mipi:      " << mipi << "\n";
    const std::string aging = ctl_text(rmsl_ctl::kCmdGetFtAging);
    if (!aging.empty())
        d << "  ft_aging:     " << aging << "\n";

    const bool pass = text_field_int(status, "overall", 0) == 1;
    ph.result = pass ? TestResult::PASS : TestResult::FAIL;
    ph.detail = d.str();
    ph.duration_sec = std::chrono::duration<double>(Clock::now()-t0).count();
    return ph;
}

/* =========================================================================
 * Phase 4: Burn-in / stress
 * ========================================================================= */

TestPhase phase_burn_in(unsigned dur_sec) {
    TestPhase ph{"Burn-in", TestResult::SKIP};
    auto t0 = Clock::now();
    std::ostringstream d; d << "  Duration: " << duration_str(dur_sec) << "\n";

    StreamSession ss[3] = {};
    for (int i = 0; i < 3; ++i) ss[i].prof = &kProfiles[i];

    int started = 0;
    for (int i = 0; i < 3; ++i) if (start_stream(&ss[i])) ++started;

    if (started < 3) { for (int i = 0; i < 3; ++i) stop_stream(&ss[i]); d << "  ERROR: not all streams started\n"; ph.result = TestResult::FAIL; ph.detail = d.str(); ph.duration_sec = std::chrono::duration<double>(Clock::now()-t0).count(); return ph; }

    auto deadline = Clock::now() + std::chrono::seconds(dur_sec);
    TimePoint last = Clock::now();
    unsigned checks = 0; bool stable = true;
    while (Clock::now() < deadline) {
        std::this_thread::sleep_for(std::chrono::seconds(1));
        if (Clock::now() - last >= std::chrono::seconds(30)) {
            ++checks;
            for (int i = 0; i < 3; ++i) { if (!ss[i].st.worker_ok.load()) { log_warn(std::string(ss[i].prof->label)+" died at #"+std::to_string(checks)); stable = false; } }
            log_info("burn-in #"+std::to_string(checks)+" rgb="+std::to_string(ss[0].st.frames.load())+" depth="+std::to_string(ss[1].st.frames.load())+" ir="+std::to_string(ss[2].st.frames.load()));
            last = Clock::now();
        }
    }

    for (int i = 0; i < 3; ++i)
        stop_stream(&ss[i]);

    d << "\n  After " << duration_str(dur_sec) << ":\n";
    for (int i = 0; i < 3; ++i)
        d << "    " << ss[i].prof->label << ": " << ss[i].st.frames.load() << " frames\n";
    d << "  " << checks << " periodic checks\n";

    ph.result = stable ? TestResult::PASS : TestResult::FAIL;
    ph.detail = d.str();
    ph.duration_sec = std::chrono::duration<double>(Clock::now()-t0).count();
    return ph;
}

/* =========================================================================
 * Output
 * ========================================================================= */

static std::string escape_json(std::string const &s) { std::string o; for (char c : s) { switch (c) { case '"': o+="\\\""; break; case '\\': o+="\\\\"; break; case '\n': o+="\\n"; break; default: o+=c; } } return o; }

static void print_json(OverallReport const &r) {
    std::cout << "{\n\"tool\":\"uvc_host_ft\",\n\"timestamp\":\""<<r.timestamp<<"\",\n\"hostname\":\""<<r.hostname<<"\",\n\"overall\":\""<<result_str(r.overall)<<"\",\n\"phases\":[\n";
    for (size_t i = 0; i < r.phases.size(); ++i) { auto &p = r.phases[i]; std::cout << "  {\"name\":\""<<p.name<<"\",\"result\":\""<<result_str(p.result)<<"\",\"duration_sec\":"<<std::fixed<<std::setprecision(1)<<p.duration_sec<<",\"detail\":\""<<escape_json(p.detail)<<"\"}"<<(i+1<r.phases.size()?",":"")<<"\n"; }
    std::cout << "]}\n";
}

static void print_text(OverallReport const &r) {
    std::cout << "\n==== RMSL321 Factory Test ====\n  Time: "<<r.timestamp<<"\n  Host: "<<r.hostname<<"\n";
    double total = 0; int pass = 0, fail = 0;
    for (auto &p : r.phases) { std::cout << "--- "<<p.name<<" --- ["<<result_str(p.result)<<"] "<<duration_str(p.duration_sec)<<"\n"<<p.detail<<"\n"; total+=p.duration_sec; if(p.result==TestResult::PASS)++pass; else if(p.result==TestResult::FAIL)++fail; }
    std::cout << "==== Total: "<<duration_str(total)<<" | "<<pass<<" PASS "<<fail<<" FAIL | RESULT: "<<result_str(r.overall)<<" ====\n";
}

/* =========================================================================
 * Main
 * ========================================================================= */

int run(int argc, char **argv) {
    RunOptions o = parse_opts(argc, argv);
    g_primary_device = o.device;
    OverallReport r; r.timestamp = now_str();
    { std::ifstream f("/proc/sys/kernel/hostname"); if (f) std::getline(f, r.hostname); while (!r.hostname.empty() && (r.hostname.back()=='\n'||r.hostname.back()=='\r')) r.hostname.pop_back(); }

    std::string ctl_err;
    (void)ctl_xu::open(&g_ctl, &ctl_err, o.device);

    if (!o.json_out) { std::cout << "RMSL321 Factory Test Tool\n=========================\n"; std::cout << "Time: "<<r.timestamp<<"\nMode: "<<(o.quick?"quick":o.long_mode?"long":"standard")<<"\n\n"; }

    auto add = [&](TestPhase p) { r.phases.push_back(p); if (!o.json_out) std::cout << "--- "<<p.name<<" --- ["<<result_str(p.result)<<"]\n"<<p.detail<<"\n"; };

    // P1
    auto p1 = phase_device_and_usb(); add(p1);
    if (p1.result == TestResult::FAIL) { r.overall = TestResult::FAIL; if (o.json_out) print_json(r); else print_text(r); return 1; }

    // P2
    auto p2 = phase_stream_test(o.stream_sec); add(p2);

    // P3
    auto p3 = phase_module_telemetry(); add(p3);

    // P4 (long only)
    if (o.long_mode) add(phase_burn_in(o.burn_in_sec));

    // Final
    r.overall = TestResult::PASS;
    for (auto &p : r.phases) if (p.result == TestResult::FAIL) r.overall = TestResult::FAIL;
    if (o.json_out) print_json(r); else print_text(r);
    return r.overall == TestResult::PASS ? 0 : 1;
}

} // namespace

int main(int argc, char **argv) { try { return run(argc, argv); } catch (std::exception const &e) { std::cerr << "FATAL: "<<e.what()<<"\n"; return 1; } }
