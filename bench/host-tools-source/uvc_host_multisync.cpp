/*
 * Copyright (C) 2026 Rockchip Electronics Co., Ltd.
 *
 * uvc_host_multisync - RMSL321 multi-module hardware-sync bring-up tool.
 *
 * The modules are frame-synchronized at sensor level: the master module
 * (SEL floating) outputs FSYNC on the sync port, slave modules (SEL to 3V3)
 * lock all three sensors to it. This tool only controls stream open ORDER
 * (master first, then slaves) and reports per-module fps / sequence lock /
 * frame arrival spread for verification.
 *
 * Usage:
 *   uvc_host_multisync --list
 *       List RMSL321 V4L2 capture devices (card names) to fill --module.
 *   uvc_host_multisync --master <idx> \
 *       --module "rgb=/dev/videoX,depth=/dev/videoY" [--module ...] \
 *       [--fps 10] [--duration 60]
 *
 * Notes:
 *   - Depth stream requires RGB+Depth both open on the module (app policy).
 *   - If a slave shows no frames, check FSYNC wiring, SEL strap and that the
 *     master was opened first.
 */

#include "v4l2_capture.hpp"

#include <atomic>
#include <chrono>
#include <climits>
#include <csignal>
#include <cstdint>
#include <cstring>
#include <algorithm>
#include <cmath>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <linux/videodev2.h>
#include <memory>
#include <mutex>
#include <numeric>
#include <optional>
#include <sstream>
#include <string>
#include <string_view>
#include <system_error>
#include <thread>
#include <unordered_map>
#include <unordered_set>
#include <vector>

using Clock = std::chrono::steady_clock;
using TimePoint = Clock::time_point;

namespace fs = std::filesystem;

namespace {

constexpr uint16_t kUvcHeadMagic = 0xA55A;
constexpr int kRgbWidth = 640;
constexpr int kRgbHeight = 540;
constexpr int kDepthWidth = 640;
constexpr int kDepthHeight = 352;
constexpr int kBufferCount = 4;
constexpr int kStatsIntervalSec = 5;
constexpr auto kStatsInterval = std::chrono::seconds(kStatsIntervalSec);
constexpr auto kPollTimeout = std::chrono::milliseconds(200);
constexpr auto kMasterFirstFrameTimeout = std::chrono::seconds(15);
constexpr auto kSlaveFirstFrameTimeout = std::chrono::seconds(20);

#pragma pack(push, 1)
/* Common prefix of UVC head V1/V2 (magic..timestamp layout identical). */
struct UvcHeadCommon {
    uint16_t magic;
    uint16_t version;
    uint16_t size;
    uint16_t type;
    uint16_t width;
    uint16_t height;
    int32_t sequence;
    int64_t timestamp; /* us, device clock */
};
#pragma pack(pop)

std::atomic<bool> g_stop{false};

struct ModuleConfig {
    std::string label;
    std::string rgb_path;
    std::string depth_path;
    bool is_master = false;
};

struct StreamStats {
    std::atomic<uint64_t> frames{0};
    std::atomic<uint64_t> drops{0};
    std::atomic<int32_t> last_seq{-1};
    std::atomic<int32_t> first_seq{-1};
    std::atomic<int64_t> last_ts{0};
    std::atomic<bool> first_frame{false};
    std::atomic<int64_t> last_arrival_ns{0};
    /* per-interval sequence set for pairing statistics (main thread swaps) */
    std::mutex seq_mu;
    std::unordered_set<int32_t> cur_seqs;
    std::unordered_set<int32_t> prev_seqs;
    /* per-frame info (sequence -> timestamp/arrival) for delta analysis */
    struct FrameInfo {
        int64_t ts_us;
        int64_t arr_ns;
    };
    std::unordered_map<int32_t, FrameInfo> cur_frames;
    std::unordered_map<int32_t, FrameInfo> prev_frames;
};

struct ModuleContext {
    ModuleConfig cfg;
    uvc_tools::V4l2Capture rgb;
    uvc_tools::V4l2Capture depth;
    StreamStats rgb_stats;
    StreamStats depth_stats;
    std::atomic<bool> rgb_running{false};
    std::atomic<bool> depth_running{false};
    std::thread rgb_thread;
    std::thread depth_thread;
    /* main-thread-only stats history */
    uint64_t rgb_last_frames = 0;
    uint64_t depth_last_frames = 0;
    std::vector<double> rgb_fps_hist;
    std::vector<double> depth_fps_hist;
};

void log_error(std::string const &where, int err = errno) {
    std::cerr << where << ": " << std::system_error(err, std::generic_category()).what() << '\n';
}

void print_usage() {
    std::cout << "usage:\n"
                 "  uvc_host_multisync --list\n"
                 "  uvc_host_multisync --master <idx> \\\n"
                 "      --module \"rgb=/dev/videoX,depth=/dev/videoY\" [--module ...] \\\n"
                 "      [--fps 10] [--duration 60]\n";
}

std::optional<UvcHeadCommon> parse_head(uvc_tools::MappedBuffer const &region, size_t bytes) {
    if (!region || bytes < sizeof(UvcHeadCommon))
        return std::nullopt;

    UvcHeadCommon head{};
    std::memcpy(&head, region.data(), sizeof(head));
    if (head.magic != kUvcHeadMagic)
        return std::nullopt;
    if (head.size < sizeof(UvcHeadCommon))
        return std::nullopt;
    return head;
}

void update_sequence_stats(StreamStats &st, int32_t sequence, int64_t ts_us, int64_t arr_ns) {
    int32_t const last = st.last_seq.load();
    if (last >= 0 && sequence > last + 1)
        st.drops.fetch_add(static_cast<uint64_t>(sequence - last - 1));
    if (st.first_seq.load() < 0)
        st.first_seq.store(sequence);
    st.last_seq.store(sequence);
    st.frames.fetch_add(1);
    st.first_frame.store(true);
    {
        std::lock_guard<std::mutex> lk(st.seq_mu);
        st.cur_seqs.insert(sequence);
        st.cur_frames[sequence] = {ts_us, arr_ns};
    }
}

void stream_loop(ModuleContext &mod, bool is_rgb) {
    uvc_tools::V4l2Capture &cap = is_rgb ? mod.rgb : mod.depth;
    StreamStats &stats = is_rgb ? mod.rgb_stats : mod.depth_stats;
    std::atomic<bool> &running = is_rgb ? mod.rgb_running : mod.depth_running;
    char const *kind = is_rgb ? "RGB" : "DEPTH";

    while (!g_stop.load() && running.load()) {
        if (!cap.poll_readable(kPollTimeout))
            continue;

        v4l2_buffer buf{};
        try {
            if (!cap.try_dequeue(buf))
                continue;
        } catch (std::system_error const &ex) {
            log_error(mod.cfg.label + ":" + kind + " DQBUF", ex.code().value());
            running.store(false);
            return;
        }

        size_t const frame_bytes = buf.bytesused > 0 ? buf.bytesused : buf.length;
        auto const head = parse_head(cap.buffer(buf.index), frame_bytes);
        int64_t const arrival_ns =
            std::chrono::duration_cast<std::chrono::nanoseconds>(Clock::now().time_since_epoch())
                .count();
        if (head) {
            update_sequence_stats(stats, head->sequence, head->timestamp, arrival_ns);
            stats.last_ts.store(head->timestamp);
        }
        stats.last_arrival_ns.store(arrival_ns);

        try {
            cap.queue(buf);
        } catch (std::system_error const &ex) {
            std::cerr << mod.cfg.label << ":" << kind << " QBUF: " << ex.what() << '\n';
            running.store(false);
            return;
        }
    }
    running.store(false);
}

bool open_stream(uvc_tools::V4l2Capture &cap,
                 std::string const &path,
                 uint32_t pixfmt,
                 int width,
                 int height,
                 int fps,
                 std::string const &label) {
    if (!cap.open(path) || !cap.configure({pixfmt, width, height, fps}, kBufferCount) ||
        !cap.start()) {
        std::cerr << '[' << label << "] ERROR: open/configure/start " << path << '\n';
        return false;
    }
    if (cap.width() != width || cap.height() != height) {
        std::cerr << '[' << label << "] WARN: driver adjusted to " << cap.width() << 'x'
                  << cap.height() << " (requested " << width << 'x' << height << ")\n";
    }
    return true;
}

bool start_module(ModuleContext &mod, int fps) {
    if (!open_stream(mod.rgb,
                     mod.cfg.rgb_path,
                     V4L2_PIX_FMT_NV12,
                     kRgbWidth,
                     kRgbHeight,
                     fps,
                     mod.cfg.label + " RGB"))
        return false;
    if (!open_stream(mod.depth,
                     mod.cfg.depth_path,
                     V4L2_PIX_FMT_YUYV,
                     kDepthWidth,
                     kDepthHeight,
                     fps,
                     mod.cfg.label + " DEPTH")) {
        mod.rgb.close();
        return false;
    }

    mod.rgb_running.store(true);
    mod.depth_running.store(true);
    mod.rgb_thread = std::thread(stream_loop, std::ref(mod), true);
    mod.depth_thread = std::thread(stream_loop, std::ref(mod), false);
    return true;
}

bool wait_first_frame(ModuleContext &mod, bool is_rgb, std::chrono::seconds timeout) {
    StreamStats &stats = is_rgb ? mod.rgb_stats : mod.depth_stats;
    TimePoint const start = Clock::now();
    while (!g_stop.load() && !stats.first_frame.load()) {
        if (Clock::now() - start > timeout)
            return false;
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }
    return stats.first_frame.load();
}

void stop_module(ModuleContext &mod) {
    mod.rgb_running.store(false);
    mod.depth_running.store(false);
    if (mod.rgb_thread.joinable())
        mod.rgb_thread.join();
    if (mod.depth_thread.joinable())
        mod.depth_thread.join();
    mod.rgb.close();
    mod.depth.close();
}

void list_devices() {
    fs::path const root = "/sys/class/video4linux";
    if (!fs::exists(root)) {
        std::cerr << "no /sys/class/video4linux\n";
        return;
    }
    std::cout << "RMSL321 capture devices:\n";
    for (auto const &entry : fs::directory_iterator(root)) {
        std::string const node = entry.path().filename().string();
        if (node.rfind("video", 0) != 0)
            continue;
        std::ifstream name_file(entry.path() / "name");
        if (!name_file)
            continue;
        std::string name;
        std::getline(name_file, name);
        if (name.find("RMSL") == std::string::npos)
            continue;
        std::cout << "  /dev/" << node << "  " << name << '\n';
    }
}

/* Accumulator for paired-frame time deltas (us). */
struct DeltaStats {
    uint64_t n = 0;
    long double sum = 0;
    long double sum2 = 0;
    int64_t min = INT64_MAX;
    int64_t max = INT64_MIN;
    /* (master_seq, delta_us) samples for percentiles / linear trend */
    std::vector<std::pair<int64_t, int64_t>> samples;

    void add(int64_t master_seq, int64_t delta_us) {
        n++;
        sum += delta_us;
        sum2 += static_cast<long double>(delta_us) * delta_us;
        min = std::min(min, delta_us);
        max = std::max(max, delta_us);
        samples.emplace_back(master_seq, delta_us);
    }

    double mean() const {
        return n ? static_cast<double>(sum / n) : 0.0;
    }

    double stddev() const {
        if (n < 2)
            return 0.0;
        double const m = mean();
        double const var = static_cast<double>(sum2 / n) - m * m;
        return var > 0 ? std::sqrt(var) : 0.0;
    }

    void merge(DeltaStats const &o) {
        n += o.n;
        sum += o.sum;
        sum2 += o.sum2;
        min = std::min(min, o.min);
        max = std::max(max, o.max);
        samples.insert(samples.end(), o.samples.begin(), o.samples.end());
    }
};

struct SyncReport {
    uint64_t paired_rgb = 0;
    uint64_t max_rgb = 0;
    uint64_t paired_depth = 0;
    uint64_t max_depth = 0;
    DeltaStats ts_rgb;
    DeltaStats ts_depth;
    DeltaStats arr_rgb;
    DeltaStats arr_depth;
};

void roll_interval_seqs(StreamStats &st) {
    std::lock_guard<std::mutex> lk(st.seq_mu);
    st.prev_seqs.swap(st.cur_seqs);
    st.cur_seqs.clear();
    st.prev_frames.swap(st.cur_frames);
    st.cur_frames.clear();
}

void print_stats(std::vector<std::unique_ptr<ModuleContext>> &modules,
                 TimePoint &last_print,
                 int interval_sec,
                 SyncReport &rep) {
    TimePoint const now = Clock::now();
    auto const elapsed = now - last_print;
    if (elapsed < std::chrono::seconds(interval_sec))
        return;
    double const elapsed_s =
        std::chrono::duration_cast<std::chrono::duration<double>>(elapsed).count();
    last_print = now;

    std::cout << "\n--- interval stats ---\n";
    for (auto &mp : modules) {
        auto &mod = *mp;
        uint64_t const rgb_now = mod.rgb_stats.frames.load();
        uint64_t const depth_now = mod.depth_stats.frames.load();
        double const rgb_fps = (rgb_now - mod.rgb_last_frames) / elapsed_s;
        double const depth_fps = (depth_now - mod.depth_last_frames) / elapsed_s;
        mod.rgb_last_frames = rgb_now;
        mod.depth_last_frames = depth_now;
        mod.rgb_fps_hist.push_back(rgb_fps);
        mod.depth_fps_hist.push_back(depth_fps);
        std::cout << '[' << mod.cfg.label << (mod.cfg.is_master ? " master" : " slave")
                  << "] RGB fps=" << std::fixed << std::setprecision(2) << rgb_fps
                  << " drops=" << mod.rgb_stats.drops.load()
                  << " | DEPTH fps=" << depth_fps
                  << " drops=" << mod.depth_stats.drops.load() << '\n';
    }

    for (auto &mp : modules) {
        roll_interval_seqs(mp->rgb_stats);
        roll_interval_seqs(mp->depth_stats);
    }

    /* Offset-aligned pairing: modules start streaming at slightly different
     * times, so their raw sequence counters differ by a constant offset.
     * We align module i's sequence space to module 0 (reference) using the
     * interval's min-sequence difference, then count frames both saw. */
    auto compute_sync = [&](bool is_rgb, SyncReport &rep)
        -> std::tuple<uint64_t, uint64_t, int64_t, int64_t> {
        StreamStats &ref_st = is_rgb ? modules[0]->rgb_stats : modules[0]->depth_stats;
        std::lock_guard<std::mutex> lk0(ref_st.seq_mu);
        if (ref_st.prev_seqs.empty())
            return {0, 0, 0, -1};

        int32_t const ref_min =
            *std::min_element(ref_st.prev_seqs.begin(), ref_st.prev_seqs.end());
        int32_t const ref_last = ref_st.last_seq.load();
        std::unordered_set<int32_t> inter = ref_st.prev_seqs;
        uint64_t max_count = ref_st.prev_seqs.size();
        int64_t offset_sum = 0;
        int offset_n = 0;
        int64_t min_aligned = ref_last;
        int64_t max_aligned = ref_last;

        DeltaStats interval_ts;
        DeltaStats interval_arr;
        for (size_t i = 1; i < modules.size(); i++) {
            StreamStats &st = is_rgb ? modules[i]->rgb_stats : modules[i]->depth_stats;
            std::lock_guard<std::mutex> lk(st.seq_mu);
            if (st.prev_seqs.empty()) {
                inter.clear();
                continue;
            }
            int32_t const mn = *std::min_element(st.prev_seqs.begin(), st.prev_seqs.end());
            int64_t const off = static_cast<int64_t>(ref_min) - mn;
            offset_sum += off;
            offset_n++;
            for (auto it = inter.begin(); it != inter.end();) {
                if (!st.prev_seqs.count(static_cast<int32_t>(*it - off)))
                    it = inter.erase(it);
                else
                    ++it;
            }
            max_count = std::max(max_count, static_cast<uint64_t>(st.prev_seqs.size()));
            int64_t const aligned_last = static_cast<int64_t>(st.last_seq.load()) + off;
            min_aligned = std::min(min_aligned, aligned_last);
            max_aligned = std::max(max_aligned, aligned_last);

            /* timestamp / host-arrival delta of same-physical-frame pairs */
            for (auto const &[seq, info] : ref_st.prev_frames) {
                auto it = st.prev_frames.find(static_cast<int32_t>(seq - off));
                if (it == st.prev_frames.end())
                    continue;
                interval_ts.add(seq, it->second.ts_us - info.ts_us);
                interval_arr.add(seq, (it->second.arr_ns - info.arr_ns) / 1000);
            }
        }
        int64_t const avg_off = offset_n ? offset_sum / offset_n : 0;
        int64_t const spread = max_aligned - min_aligned;
        /* Only trust delta samples from intervals whose frame numbers are
         * tightly aligned (first interval often mis-estimates the offset
         * while streams are still ramping up). */
        if (spread <= 2) {
            DeltaStats &ts_d = is_rgb ? rep.ts_rgb : rep.ts_depth;
            DeltaStats &arr_d = is_rgb ? rep.arr_rgb : rep.arr_depth;
            ts_d.merge(interval_ts);
            arr_d.merge(interval_arr);
        }
        return {inter.size(), max_count, avg_off, spread};
    };

    auto arrival_spread_us = [&](bool is_rgb) -> int64_t {
        int64_t min_ns = INT64_MAX;
        int64_t max_ns = INT64_MIN;
        int seen = 0;
        for (auto &mp : modules) {
            auto &mod = *mp;
            StreamStats &st = is_rgb ? mod.rgb_stats : mod.depth_stats;
            int64_t const a = st.last_arrival_ns.load();
            if (a <= 0)
                continue;
            min_ns = std::min(min_ns, a);
            max_ns = std::max(max_ns, a);
            seen++;
        }
        if (seen < 2)
            return -1;
        return (max_ns - min_ns) / 1000;
    };

    auto [pr, mr, off_rgb, spread_rgb] = compute_sync(true, rep);
    auto [pd, md, off_depth, spread_depth] = compute_sync(false, rep);
    rep.paired_rgb += pr;
    rep.max_rgb += mr;
    rep.paired_depth += pd;
    rep.max_depth += md;

    double const rate_rgb = mr ? 100.0 * static_cast<double>(pr) / mr : 0.0;
    double const rate_depth = md ? 100.0 * static_cast<double>(pd) / md : 0.0;
    std::cout << "sync: RGB paired=" << pr << "/" << mr << " rate=" << std::fixed
              << std::setprecision(1) << rate_rgb << "% offset=" << off_rgb
              << " aligned_spread=" << spread_rgb
              << " | DEPTH paired=" << pd << "/" << md << " rate=" << rate_depth
              << "% offset=" << off_depth << " aligned_spread=" << spread_depth
              << " | arrival_spread_us RGB=" << arrival_spread_us(true)
              << " DEPTH=" << arrival_spread_us(false) << '\n';

    auto print_delta = [](char const *kind, DeltaStats const &d) {
        if (d.n == 0) {
            std::cout << "ts_delta " << kind << ": n=0\n";
            return;
        }
        std::cout << "ts_delta " << kind << ": n=" << d.n << " mean=" << std::fixed
                  << std::setprecision(1) << d.mean() << "us std=" << d.stddev()
                  << "us min=" << d.min << "us max=" << d.max << "us\n";
    };
    print_delta("RGB", rep.ts_rgb);
    print_delta("DEPTH", rep.ts_depth);
}

} // namespace

int main(int argc, char **argv) {
    bool list_only = false;
    int master_idx = 0;
    int fps = 10;
    int duration_sec = 0;
    int interval_sec = kStatsIntervalSec;
    std::vector<ModuleConfig> modules;

    for (int i = 1; i < argc; i++) {
        std::string const arg = argv[i];
        if (arg == "--list") {
            list_only = true;
        } else if (arg == "--master" && i + 1 < argc) {
            master_idx = std::atoi(argv[++i]);
        } else if (arg == "--fps" && i + 1 < argc) {
            fps = std::atoi(argv[++i]);
        } else if (arg == "--duration" && i + 1 < argc) {
            duration_sec = std::atoi(argv[++i]);
        } else if (arg == "--interval" && i + 1 < argc) {
            interval_sec = std::atoi(argv[++i]);
        } else if (arg == "--module" && i + 1 < argc) {
            ModuleConfig cfg;
            cfg.label = "M" + std::to_string(modules.size());
            std::istringstream kv(argv[++i]);
            std::string pair;
            while (std::getline(kv, pair, ',')) {
                auto const eq = pair.find('=');
                if (eq == std::string::npos)
                    continue;
                std::string const key = pair.substr(0, eq);
                std::string const value = pair.substr(eq + 1);
                if (key == "rgb")
                    cfg.rgb_path = value;
                else if (key == "depth")
                    cfg.depth_path = value;
                else if (key == "label")
                    cfg.label = value;
            }
            modules.push_back(std::move(cfg));
        } else if (arg == "-h" || arg == "--help") {
            print_usage();
            return 0;
        } else {
            std::cerr << "unknown option: " << arg << '\n';
            print_usage();
            return 1;
        }
    }

    if (list_only) {
        list_devices();
        return 0;
    }

    if (modules.empty()) {
        print_usage();
        return 1;
    }
    for (auto &cfg : modules) {
        if (cfg.rgb_path.empty() || cfg.depth_path.empty()) {
            std::cerr << '[' << cfg.label << "] needs rgb= and depth= paths\n";
            return 1;
        }
    }
    if (master_idx < 0 || master_idx >= static_cast<int>(modules.size())) {
        std::cerr << "--master must be a valid module index (0.." << modules.size() - 1 << ")\n";
        return 1;
    }
    modules[static_cast<size_t>(master_idx)].is_master = true;

    std::signal(SIGINT, +[](int) { g_stop.store(true); });
    std::signal(SIGTERM, +[](int) { g_stop.store(true); });

    std::vector<std::unique_ptr<ModuleContext>> ctxs;
    ctxs.reserve(modules.size());
    for (auto &cfg : modules) {
        auto m = std::make_unique<ModuleContext>();
        m->cfg = cfg;
        ctxs.push_back(std::move(m));
    }

    std::cout << "opening master first (" << ctxs[static_cast<size_t>(master_idx)]->cfg.label
              << "), then slaves...\n";

    bool ok = true;
    for (size_t i = 0; i < ctxs.size() && ok; i++) {
        if (!start_module(*ctxs[i], fps)) {
            std::cerr << '[' << ctxs[i]->cfg.label << "] start failed\n";
            ok = false;
            break;
        }
        bool const is_master = ctxs[i]->cfg.is_master;
        auto const timeout =
            is_master ? kMasterFirstFrameTimeout : kSlaveFirstFrameTimeout;
        bool const rgb_ok = wait_first_frame(*ctxs[i], true, timeout);
        bool const depth_ok = wait_first_frame(*ctxs[i], false, timeout);
        std::cout << '[' << ctxs[i]->cfg.label
                  << (is_master ? " master" : " slave") << "] first frame: RGB="
                  << (rgb_ok ? "yes" : "NO") << " DEPTH=" << (depth_ok ? "yes" : "NO") << '\n';
        if (is_master && (!rgb_ok || !depth_ok)) {
            std::cerr << "ERROR: master module did not produce frames\n";
            ok = false;
        } else if (!rgb_ok || !depth_ok) {
            std::cerr << "WARN: slave module has no frames - check FSYNC wiring / SEL strap / "
                         "open order\n";
        }
    }

    if (!ok) {
        g_stop.store(true);
        for (auto &mp : ctxs)
            stop_module(*mp);
        return 1;
    }

    TimePoint const start_time = Clock::now();
    TimePoint last_print = Clock::now();
    SyncReport rep;
    std::cout << "all modules streaming. Ctrl+C to stop.\n";

    while (!g_stop.load()) {
        if (duration_sec > 0 &&
            std::chrono::duration_cast<std::chrono::seconds>(Clock::now() - start_time).count() >=
                duration_sec)
            break;
        bool any_dead = false;
        for (auto &mp : ctxs)
            any_dead = any_dead || !mp->rgb_running.load() || !mp->depth_running.load();
        if (any_dead) {
            std::cerr << "a stream stopped unexpectedly\n";
            break;
        }
        print_stats(ctxs, last_print, interval_sec, rep);
        std::this_thread::sleep_for(std::chrono::milliseconds(200));
    }

    g_stop.store(true);
    /* Stop slaves before master to respect the sync open/close order. */
    for (auto &mp : ctxs) {
        if (!mp->cfg.is_master)
            stop_module(*mp);
    }
    for (auto &mp : ctxs) {
        if (mp->cfg.is_master)
            stop_module(*mp);
    }

    double const elapsed_s =
        std::chrono::duration_cast<std::chrono::duration<double>>(Clock::now() - start_time)
            .count();
    std::cout << "\n--- final summary (" << std::fixed << std::setprecision(1) << elapsed_s
              << "s) ---\n";
    for (auto &mp : ctxs) {
        auto &m = *mp;
        auto fps_summary = [](std::vector<double> const &hist, uint64_t total, double sec) {
            std::ostringstream oss;
            oss << std::fixed << std::setprecision(2);
            if (hist.empty()) {
                oss << "avg=n/a";
                return oss.str();
            }
            double const sum = std::accumulate(hist.begin(), hist.end(), 0.0);
            double const avg = sum / hist.size();
            double const mn = *std::min_element(hist.begin(), hist.end());
            double const mx = *std::max_element(hist.begin(), hist.end());
            oss << "avg=" << avg << " min=" << mn << " max=" << mx
                << " overall=" << (sec > 0 ? total / sec : 0.0);
            return oss.str();
        };
        std::cout << '[' << m.cfg.label << (m.cfg.is_master ? " master" : " slave") << "] RGB "
                  << fps_summary(m.rgb_fps_hist, m.rgb_stats.frames.load(), elapsed_s)
                  << " drops=" << m.rgb_stats.drops.load() << " | DEPTH "
                  << fps_summary(m.depth_fps_hist, m.depth_stats.frames.load(), elapsed_s)
                  << " drops=" << m.depth_stats.drops.load() << '\n';
    }
    double const rate_rgb =
        rep.max_rgb ? 100.0 * static_cast<double>(rep.paired_rgb) / rep.max_rgb : 0.0;
    double const rate_depth =
        rep.max_depth ? 100.0 * static_cast<double>(rep.paired_depth) / rep.max_depth : 0.0;
    std::cout << "pairing overall: RGB " << rep.paired_rgb << "/" << rep.max_rgb << " ("
              << std::fixed << std::setprecision(1) << rate_rgb << "%) | DEPTH "
              << rep.paired_depth << "/" << rep.max_depth << " (" << rate_depth << "%)\n";

    auto percentile = [](std::vector<int64_t> v, double q) -> double {
        if (v.empty())
            return 0.0;
        std::sort(v.begin(), v.end());
        double const pos = q * static_cast<double>(v.size() - 1);
        size_t const lo = static_cast<size_t>(pos);
        size_t const hi = std::min(lo + 1, v.size() - 1);
        double const frac = pos - static_cast<double>(lo);
        return static_cast<double>(v[lo]) * (1.0 - frac) + static_cast<double>(v[hi]) * frac;
    };

    auto print_delta_summary = [&](char const *kind, DeltaStats const &d) {
        if (d.n == 0) {
            std::cout << kind << " delta: n=0\n";
            return;
        }
        std::vector<int64_t> raw;
        raw.reserve(d.samples.size());
        long double sx = 0, sy = 0, sxx = 0, sxy = 0;
        for (auto const &[seq, del] : d.samples) {
            raw.push_back(del);
            sx += seq;
            sy += del;
            sxx += static_cast<long double>(seq) * seq;
            sxy += static_cast<long double>(seq) * del;
        }
        double const slope =
            d.n > 1 ? static_cast<double>((d.n * sxy - sx * sy) /
                                          (d.n * sxx - sx * sx))
                    : 0.0;
        double const intercept = d.n ? static_cast<double>(sy / d.n) - slope * static_cast<double>(sx / d.n) : 0.0;
        long double rs = 0, rs2 = 0;
        int64_t rmin = INT64_MAX, rmax = INT64_MIN;
        std::vector<int64_t> resid;
        resid.reserve(d.samples.size());
        for (auto const &[seq, del] : d.samples) {
            double const r = static_cast<double>(del) - (intercept + slope * seq);
            rs += r;
            rs2 += static_cast<long double>(r) * r;
            int64_t const ri = static_cast<int64_t>(std::llround(r));
            resid.push_back(ri);
            rmin = std::min(rmin, ri);
            rmax = std::max(rmax, ri);
        }
        double const rstd = d.n > 1 ? std::sqrt(std::max(0.0L, static_cast<long double>(rs2 / d.n) -
                                                                     (rs / d.n) * (rs / d.n)))
                                    : 0.0;
        std::cout << kind << " delta [us]: n=" << d.n << " mean=" << std::fixed
                  << std::setprecision(1) << d.mean() << " std=" << d.stddev()
                  << " min=" << d.min << " max=" << d.max << " p50=" << percentile(raw, 0.50)
                  << " p95=" << percentile(raw, 0.95) << " p99=" << percentile(raw, 0.99)
                  << " | trend=" << std::setprecision(3) << slope << "us/frame"
                  << " | residual_std=" << std::setprecision(1) << rstd << "us min=" << rmin
                  << " max=" << rmax << " p50=" << percentile(resid, 0.50)
                  << " p95=" << percentile(resid, 0.95) << " p99=" << percentile(resid, 0.99)
                  << '\n';
    };
    print_delta_summary("ts", rep.ts_rgb);
    print_delta_summary("ts", rep.ts_depth);
    print_delta_summary("arrival", rep.arr_rgb);
    print_delta_summary("arrival", rep.arr_depth);

    std::cout << "RESULT: " << (ok ? "PASS" : "FAIL") << '\n';
    return ok ? 0 : 1;
}
