/*
 * uvc_host_imu — RMSL321 IMU statistics over HID.
 *
 * The module writes IMU samples to the HID gadget as input reports
 * (report id 1, 49 bytes = outer report id byte + two 24-byte packed
 * samples).  This tool reads the
 * matching /dev/hidrawN node and prints per-window statistics: sample rate
 * (median timestamp delta), timestamp jitter (1-50 ms deltas) and six-axis /
 * temperature averages.
 *
 * usage: uvc_host_imu [duration_sec] [window_sec]
 */

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <fcntl.h>
#include <string>
#include <time.h>
#include <unistd.h>
#include <vector>

namespace {

#pragma pack(push, 1)
struct hid_imu_report {
    uint8_t report_id;
    uint8_t sequence;
    int16_t ax;
    int16_t ay;
    int16_t az;
    int16_t gx;
    int16_t gy;
    int16_t gz;
    int16_t temp;
    uint64_t timestamp;
};
#pragma pack(pop)
static_assert(sizeof(hid_imu_report) == 24, "hid_imu_report layout mismatch");

constexpr size_t kReportBytes = 49; /* outer report id byte + two packed samples */

std::string read_sysfs(const std::string &path) {
    FILE *f = ::fopen(path.c_str(), "r");
    if (!f)
        return {};
    char buf[64] = {0};
    const size_t n = ::fread(buf, 1, sizeof(buf) - 1, f);
    ::fclose(f);
    return std::string(buf, n);
}

/* Find the module's hidraw node (USB vendor 0x2207). */
std::string find_hidraw() {
    for (int i = 0; i < 16; ++i) {
        const std::string base = "/sys/class/hidraw/hidraw" + std::to_string(i);
        std::string sys = base + "/device/../../idVendor";
        std::string vendor = read_sysfs(sys);
        if (vendor.empty()) {
            sys = base + "/device/idVendor";
            vendor = read_sysfs(sys);
        }
        if (!vendor.empty() && vendor.find("2207") != std::string::npos)
            return "/dev/hidraw" + std::to_string(i);
    }
    return {};
}

struct WindowStats {
    uint64_t n = 0;
    double sum_ax = 0, sum_ay = 0, sum_az = 0;
    double sum_gx = 0, sum_gy = 0, sum_gz = 0, sum_temp = 0;
    std::vector<double> deltas;
    std::vector<double> jitter;

    void reset() {
        n = 0;
        sum_ax = sum_ay = sum_az = 0;
        sum_gx = sum_gy = sum_gz = sum_temp = 0;
        deltas.clear();
        jitter.clear();
    }

    void add(const hid_imu_report &s, uint64_t &last_ts) {
        ++n;
        sum_ax += s.ax / 1000.0;
        sum_ay += s.ay / 1000.0;
        sum_az += s.az / 1000.0;
        sum_gx += s.gx / 1000.0;
        sum_gy += s.gy / 1000.0;
        sum_gz += s.gz / 1000.0;
        sum_temp += s.temp / 100.0;
        if (last_ts && s.timestamp >= last_ts) {
            const double d = (double)(s.timestamp - last_ts);
            deltas.push_back(d);
            if (d >= 1000 && d <= 50000)
                jitter.push_back(d);
        }
        last_ts = s.timestamp;
    }

    void print(const char *tag) const {
        double rate = 0, javg = 0, jsd = 0;
        if (!deltas.empty()) {
            std::vector<double> d = deltas;
            std::sort(d.begin(), d.end());
            const double med = d[d.size() / 2];
            if (med > 0)
                rate = 1e6 / med;
        }
        if (!jitter.empty()) {
            double sum = 0, sq = 0;
            for (double v : jitter) {
                sum += v;
                sq += v * v;
            }
            javg = sum / jitter.size();
            const double var = (sq / jitter.size()) - javg * javg;
            jsd = var > 0 ? std::sqrt(var) : 0;
        }
        printf("[%s] samples=%llu rate=%.1f Hz jitter(us) avg=%.1f sd=%.2f\n",
               tag,
               (unsigned long long)n,
               rate,
               javg,
               jsd);
        if (n > 0)
            printf("[%s] accel(g)=%+.3f,%+.3f,%+.3f gyro(dps)=%+.3f,%+.3f,%+.3f temp=%.2fC\n",
                   tag,
                   sum_ax / n,
                   sum_ay / n,
                   sum_az / n,
                   sum_gx / n,
                   sum_gy / n,
                   sum_gz / n,
                   sum_temp / n);
    }
};

double mono_now() {
    struct timespec ts;
    ::clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec + ts.tv_nsec / 1e9;
}

} // namespace

int main(int argc, char **argv) {
    double secs = 30.0, win_sec = 5.0;
    std::string dev;
    std::vector<double> pos;
    for (int i = 1; i < argc; ++i) {
        std::string const a = argv[i];
        if (a == "--hidraw" && i + 1 < argc)
            dev = argv[++i];
        else
            pos.push_back(std::atof(argv[i]));
    }
    if (!pos.empty())
        secs = pos[0];
    if (pos.size() > 1)
        win_sec = pos[1];
    if (dev.empty())
        dev = find_hidraw();
    if (dev.empty()) {
        std::fprintf(stderr, "no RMSL321 HID node found (is the module connected?)\n");
        return 1;
    }
    const int fd = ::open(dev.c_str(), O_RDONLY | O_NONBLOCK | O_CLOEXEC);
    if (fd < 0) {
        std::fprintf(stderr, "cannot open %s: %s\n", dev.c_str(), std::strerror(errno));
        return 1;
    }

    WindowStats win;
    uint64_t last_ts = 0;
    uint32_t clean_deltas = 0;
    uint64_t settle_count = 0, frames = 0;
    const double t0 = mono_now();
    double win_start = t0;
    int win_idx = 0;

    printf("device: %s, duration=%.0fs, window=%.0fs\n", dev.c_str(), secs, win_sec);
    while (true) {
        const double now = mono_now();
        if (now - t0 >= secs)
            break;

        uint8_t buf[kReportBytes];
        const ssize_t n = ::read(fd, buf, sizeof(buf));
        if (n < 0) {
            if (errno == EAGAIN || errno == EWOULDBLOCK) {
                ::usleep(2000);
                continue;
            }
            std::fprintf(stderr, "hid read error: %s\n", std::strerror(errno));
            break;
        }
        if (n <= 0)
            continue;
        /* First byte is the outer report id; rest is the 48-byte payload. */
        if (buf[0] != 0x01)
            continue;
        const size_t samples = ((size_t)n - 1) / sizeof(hid_imu_report);
        if (samples == 0)
            continue;
        ++frames;
        for (size_t i = 0; i < samples; ++i) {
            hid_imu_report s;
            std::memcpy(&s, buf + 1 + i * sizeof(s), sizeof(s));
            if (s.report_id != 0x01)
                continue;
            /* Settle: three consecutive 4-6 ms deltas => real-time. */
            if (last_ts && s.timestamp >= last_ts) {
                const double d = (double)(s.timestamp - last_ts);
                if (d >= 4000 && d <= 6000)
                    ++clean_deltas;
                else
                    clean_deltas = 0;
            }
            if (clean_deltas < 3) {
                ++settle_count;
                last_ts = s.timestamp;
                continue;
            }
            win.add(s, last_ts);
        }

        if (now - win_start >= win_sec) {
            char tag[16];
            std::snprintf(tag, sizeof(tag), "win %02d", ++win_idx);
            win.print(tag);
            win.reset();
            win_start = now;
        }
    }
    if (win.n > 0)
        win.print("last");
    printf("done: reports=%llu, settle=%llu dropped\n",
           (unsigned long long)frames,
           (unsigned long long)settle_count);
    ::close(fd);
    return 0;
}
