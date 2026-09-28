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
 * RMSL321 UVC XU control channel client. Owns ALL control operations (the
 * stream tool uvc_host_stream is deliberately limited to capturing frames).
 *
 * Build (from tools/):
 *   cmake -B build && cmake --build build
 *   ./build/uvc_host_ctl --check
 *   ./build/uvc_host_ctl set-algo-mode 640x352
 */

#include "ctl_proto.h"
#include "ctl_xu.hpp"

#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fcntl.h>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <sstream>
#include <string>
#include <thread>
#include <unistd.h>
#include <vector>

using Clock = std::chrono::steady_clock;

namespace {

using XuLink = ctl_xu::Link;

std::string trim(std::string s) {
    while (!s.empty() && (s.back() == '\n' || s.back() == '\r'))
        s.pop_back();
    return s;
}

bool xu_open(XuLink *link, std::string *err, std::string const &device = {}) {
    return ctl_xu::open(link, err, device);
}

void xu_close(XuLink *link) {
    ctl_xu::close(link);
}

/* Plain GET (select + read all chunks). */
bool xu_get(XuLink *link, uint32_t cmd, std::vector<uint8_t> *resp) {
    return ctl_xu::get(link, cmd, resp);
}

/* GET with command-specific select data (e.g. a file path). */
bool xu_get_extra(XuLink *link, uint32_t cmd, const std::vector<uint8_t> &extra,
                  std::vector<uint8_t> *resp) {
    return ctl_xu::get_extra(link, cmd, extra, resp);
}

/* SET then read the single-chunk response (for commands with a result). */
bool xu_set_get(XuLink *link,
                uint32_t cmd,
                const std::vector<uint8_t> &payload,
                std::vector<uint8_t> *resp) {
    return ctl_xu::set_get(link, cmd, payload, resp);
}

bool xu_ping(XuLink *link, double *rtt_ms) {
    return ctl_xu::ping(link, rtt_ms);
}

std::string resp_text(const std::vector<uint8_t> &resp) {
    return trim(std::string(resp.begin(), resp.end()));
}

/* ---------------- command implementations ---------------- */

int cmd_ping(XuLink *link, int count) {
    bool ok = true;
    double sum = 0;
    double best = 1e9;
    for (int i = 0; i < count; ++i) {
        double rtt = 0;
        if (!xu_ping(link, &rtt)) {
            ok = false;
            break;
        }
        sum += rtt;
        best = std::min(best, rtt);
        std::printf("ping %d: %.2f ms\n", i + 1, rtt);
    }
    if (ok && count > 0) {
        std::printf("summary: avg=%.2f ms min=%.2f ms\n", sum / count, best);
        return 0;
    }
    return 1;
}

int cmd_get(XuLink *link, uint32_t cmd) {
    std::vector<uint8_t> resp;
    if (!xu_get(link, cmd, &resp)) {
        std::cerr << "command failed: "
                  << (resp.empty() ? "no response" : resp_text(resp)) << "\n";
        return 1;
    }
    std::cout << resp_text(resp) << "\n";
    return 0;
}

int cmd_file_list(XuLink *link, const std::string &dir) {
    std::vector<uint8_t> extra(dir.begin(), dir.end());
    std::vector<uint8_t> resp;
    if (!xu_get_extra(link, rmsl_ctl::kCmdFileList, extra, &resp)) {
        std::cerr << "file-list failed: " << resp_text(resp) << "\n";
        return 1;
    }
    std::cout.write(reinterpret_cast<const char *>(resp.data()), (std::streamsize)resp.size());
    return 0;
}

int cmd_file_get(XuLink *link, const std::string &path, const std::string &outfile) {
    std::vector<uint8_t> extra(path.begin(), path.end());
    std::vector<uint8_t> resp;
    if (!xu_get_extra(link, rmsl_ctl::kCmdFileGet, extra, &resp)) {
        std::cerr << "file-get failed: " << resp_text(resp) << "\n";
        return 1;
    }
    if (outfile.empty()) {
        std::cout.write(reinterpret_cast<const char *>(resp.data()), (std::streamsize)resp.size());
        std::cout << "\n";
    } else {
        std::ofstream f(outfile, std::ios::binary);
        if (!f) {
            std::cerr << "cannot write " << outfile << "\n";
            return 1;
        }
        f.write(reinterpret_cast<const char *>(resp.data()), (std::streamsize)resp.size());
        std::cout << "saved " << resp.size() << " bytes to " << outfile << "\n";
    }
    return 0;
}

int cmd_file_put(XuLink *link, const std::string &local, const std::string &remote) {
    std::ifstream f(local, std::ios::binary | std::ios::ate);
    if (!f) {
        std::cerr << "cannot open " << local << "\n";
        return 1;
    }
    const std::streamoff size = f.tellg();
    if (size < 0 || (size_t)size > rmsl_ctl::kMaxPayload) {
        std::cerr << "file too large (max " << rmsl_ctl::kMaxPayload << " bytes)\n";
        return 1;
    }
    f.seekg(0, std::ios::beg);
    std::vector<uint8_t> data((size_t)size);
    if (size > 0)
        f.read(reinterpret_cast<char *>(data.data()), size);

    std::vector<uint8_t> resp;
    const size_t chunk_cap = rmsl_xu::kCtrlPayload;

    std::vector<uint8_t> open_payload(remote.begin(), remote.end());
    open_payload.push_back('\0');
    /* Size hint so the module knows where the real data ends (the SET
     * channel is zero-padded to its fixed 60-byte size). */
    const uint32_t size_hint = static_cast<uint32_t>(size);
    const uint8_t *size_bytes = reinterpret_cast<const uint8_t *>(&size_hint);
    open_payload.insert(open_payload.end(), size_bytes, size_bytes + sizeof(size_hint));
    if (!xu_set_get(link, rmsl_ctl::kCmdFilePutOpen, open_payload, &resp)) {
        std::cerr << "file-put open failed: " << resp_text(resp) << "\n";
        return 1;
    }

    size_t off = 0;
    while (off < data.size()) {
        const size_t n = std::min(chunk_cap, data.size() - off);
        std::vector<uint8_t> chunk(data.begin() + off, data.begin() + off + n);
        if (!xu_set_get(link, rmsl_ctl::kCmdFilePutChunk, chunk, &resp)) {
            std::cerr << "file-put chunk failed: " << resp_text(resp) << "\n";
            return 1;
        }
        off += n;
    }

    if (!xu_set_get(link, rmsl_ctl::kCmdFilePutClose, {}, &resp)) {
        std::cerr << "file-put close failed: " << resp_text(resp) << "\n";
        return 1;
    }
    std::cout << "wrote " << data.size() << " bytes to " << remote << "\n";
    return 0;
}

int cmd_get_param(XuLink *link) {
    std::vector<uint8_t> resp;
    if (!xu_get(link, rmsl_ctl::kCmdGetParam, &resp)) {
        std::cerr << "get-param failed\n";
        return 1;
    }
    std::cout.write(reinterpret_cast<const char *>(resp.data()), (std::streamsize)resp.size());
    return 0;
}

int cmd_set_algo_mode(XuLink *link, const std::string &target) {
    std::vector<uint8_t> payload(target.begin(), target.end());
    std::vector<uint8_t> resp;
    if (!xu_set_get(link, rmsl_ctl::kCmdSetAlgoMode, payload, &resp)) {
        std::cerr << "set-algo-mode failed: " << resp_text(resp) << "\n";
        return 1;
    }
    std::cout << "algo mode set to " << target << ", module rebooting\n";
    return 0;
}

int cmd_get_algo_mode(XuLink *link) {
    return cmd_get(link, rmsl_ctl::kCmdGetAlgoMode);
}

int cmd_get_log(XuLink *link) {
    std::vector<uint8_t> resp;
    if (!xu_get(link, rmsl_ctl::kCmdGetLog, &resp)) {
        std::cerr << "get-log failed: " << resp_text(resp) << "\n";
        return 1;
    }
    std::cout.write(reinterpret_cast<const char *>(resp.data()), (std::streamsize)resp.size());
    return 0;
}

int cmd_clean_log(XuLink *link) {
    std::vector<uint8_t> resp;
    if (!xu_set_get(link, rmsl_ctl::kCmdCleanLog, {}, &resp)) {
        std::cerr << "clean-log failed: " << resp_text(resp) << "\n";
        return 1;
    }
    std::cout << resp_text(resp) << "\n";
    return 0;
}

int cmd_status(XuLink *link, uint32_t cmd) {
    std::vector<uint8_t> resp;
    if (!xu_get(link, cmd, &resp)) {
        std::cerr << "status command failed\n";
        return 1;
    }
    std::cout.write(reinterpret_cast<const char *>(resp.data()), (std::streamsize)resp.size());
    if (!resp.empty() && resp.back() != '\n')
        std::cout << '\n';
    return 0;
}

/* One-shot text query; returns "" on failure. */
std::string xu_text(XuLink *link, uint32_t cmd) {
    std::vector<uint8_t> resp;
    if (!xu_get(link, cmd, &resp))
        return "";
    return resp_text(resp);
}

int cmd_health(XuLink *link) {
    std::cout << "=== RMSL321 health ===\n";

    double rtt = 0;
    std::cout << "usb link: " << (xu_ping(link, &rtt) ? "UP" : "DOWN");
    if (rtt > 0)
        std::cout << " (rtt=" << std::fixed << std::setprecision(0) << rtt << "ms)";
    std::cout << "\n";

    const std::string app = xu_text(link, rmsl_ctl::kCmdGetAppStatus);
    std::cout << "app: "
              << (app.find("app: running") != std::string::npos ? "RUNNING" : "not running")
              << "\n";

    const std::string sensor = xu_text(link, rmsl_ctl::kCmdGetSensorStatus);
    int scnt = 0;
    if (std::sscanf(sensor.c_str(), "sensor: sc132gs count=%d", &scnt) != 1)
        scnt = -1;
    std::cout << "sensor: " << scnt << "/3 SC132GS "
              << (scnt == 3 ? "OK" : "MISSING (check camera board)") << "\n";

    const std::string imu = xu_text(link, rmsl_ctl::kCmdGetImuStatus);
    int icnt = 0;
    const std::size_t p = imu.find("count=");
    if (p != std::string::npos)
        icnt = std::atoi(imu.c_str() + p + 6);
    else
        icnt = -1;
    std::cout << "imu: " << (icnt >= 1 ? "OK" : "MISSING (camera board)") << "\n";

    const std::string algo = xu_text(link, rmsl_ctl::kCmdGetAlgoMode);
    if (!algo.empty())
        std::cout << "algo mode: " << algo << "\n";
    return 0;
}

int cmd_control(XuLink *link, uint32_t cmd, const std::vector<uint8_t> &payload = {}) {
    /* Commands that reboot / tear the gadget down may reboot before the GET
     * response arrives; send them fire-and-forget and verify externally. */
    if (cmd == rmsl_ctl::kCtlCmdReboot || cmd == rmsl_ctl::kCtlCmdRebootLoader ||
        cmd == rmsl_ctl::kCtlCmdEnterDfu || cmd == rmsl_ctl::kCmdRestartApp) {
        if (!ctl_xu::set(link, cmd, payload))
            std::cerr << "control command send failed\n";
        return 0;
    }
    std::vector<uint8_t> resp;
    if (!xu_set_get(link, cmd, payload, &resp)) {
        std::cerr << "control command failed"
                  << (resp.empty() ? "" : ": " + resp_text(resp)) << "\n";
        return 1;
    }
    return 0;
}

/* Raw GET for app-live commands (FOV family) that answer a single
 * un-prefixed 60-byte payload instead of the chunked ctl_service format. */
bool xu_raw_get(XuLink *link, uint32_t cmd, std::vector<uint8_t> *resp) {
    uint16_t ctrl_len = 0;
    if (!rmsl_xu::xu_query_len(link->fd, rmsl_xu::kSelector, &ctrl_len))
        return false;
    if (ctrl_len > rmsl_xu::kDefaultLen)
        ctrl_len = rmsl_xu::kDefaultLen;
    const std::vector<uint8_t> sel = rmsl_xu::select_payload(cmd);
    if (!rmsl_xu::xu_set_padded(link->fd, &link->usb, sel.data(),
                                static_cast<uint16_t>(sel.size()), ctrl_len))
        return false;
    resp->resize(ctrl_len);
    return rmsl_xu::xu_get_padded(link->fd, &link->usb, rmsl_xu::kSelector,
                                  resp->data(), ctrl_len, ctrl_len);
}

int cmd_set_int(XuLink *link, uint32_t cmd, int64_t v) {
    const int32_t val = static_cast<int32_t>(v);
    std::vector<uint8_t> payload(reinterpret_cast<const uint8_t *>(&val),
                                 reinterpret_cast<const uint8_t *>(&val) + sizeof(val));
    return cmd_control(link, cmd, payload);
}

int cmd_set_float(XuLink *link, uint32_t cmd, float v) {
    std::vector<uint8_t> payload(reinterpret_cast<const uint8_t *>(&v),
                                 reinterpret_cast<const uint8_t *>(&v) + sizeof(v));
    return cmd_control(link, cmd, payload);
}

int cmd_set_str(XuLink *link, uint32_t cmd, const std::string &s) {
    std::vector<uint8_t> payload(s.begin(), s.end());
    return cmd_control(link, cmd, payload);
}

int cmd_set_ft_aging(XuLink *link, const std::string &s) {
    const int v = std::atoi(s.c_str());
    if (v < -2 || v > 1) {
        std::cerr << "invalid aging result: " << s << " (use -2|-1|0|1)\n";
        return 1;
    }
    /* Device aging parser accepts only the text form: the UVC channel pads
     * SET payloads to 60 B, so the 4-byte-int branch never matches. */
    const std::string text = std::to_string(v);
    std::vector<uint8_t> payload(text.begin(), text.end());
    std::vector<uint8_t> resp;
    if (!xu_set_get(link, rmsl_ctl::kCmdSetFtAging, payload, &resp)) {
        std::cerr << "set-ft-aging failed: " << resp_text(resp) << "\n";
        return 1;
    }
    std::cout << resp_text(resp) << "\n";
    return 0;
}

int cmd_get_fov_current(XuLink *link) {
    std::vector<uint8_t> resp;
    if (!xu_raw_get(link, 0x00000210u, &resp) || resp.size() < sizeof(float)) {
        std::cerr << "get-fov failed\n";
        return 1;
    }
    float fov = 0;
    std::memcpy(&fov, resp.data(), sizeof(fov));
    std::cout << std::fixed << std::setprecision(2) << fov << "\n";
    return 0;
}

int cmd_get_fov_list(XuLink *link) {
    std::vector<uint8_t> resp;
    if (!xu_raw_get(link, 0x00000211u, &resp) || resp.size() < 4) {
        std::cerr << "get-fov-list failed\n";
        return 1;
    }
    const uint32_t n = *reinterpret_cast<const uint32_t *>(resp.data());
    std::cout << "fov list (" << n << "):";
    for (uint32_t i = 0; i < n && 4 + (i + 1) * sizeof(float) <= resp.size(); ++i) {
        float f = 0;
        std::memcpy(&f, resp.data() + 4 + i * sizeof(float), sizeof(f));
        std::cout << " " << std::fixed << std::setprecision(2) << f;
    }
    std::cout << "\n";
    return 0;
}

int cmd_get_fov_crop(XuLink *link) {
    std::vector<uint8_t> resp;
    if (!xu_raw_get(link, 0x00000212u, &resp)) {
        std::cerr << "get-fov-crop failed\n";
        return 1;
    }
    while (!resp.empty() && resp.back() == '\0')
        resp.pop_back();
    std::cout << resp_text(resp) << "\n";
    return 0;
}

/* Time sync: app-live SET with an 8-byte little-endian host timestamp (us). */
int cmd_time_sync(XuLink *link, const std::string &s) {
    const uint64_t us = strtoull(s.c_str(), nullptr, 0);
    std::vector<uint8_t> payload(reinterpret_cast<const uint8_t *>(&us),
                                 reinterpret_cast<const uint8_t *>(&us) + sizeof(us));
    if (!ctl_xu::set(link, 0xFF000000u, payload)) {
        std::cerr << "time-sync send failed\n";
        return 1;
    }
    std::cout << "time-sync sent (" << us << " us)\n";
    return 0;
}


int cmd_adb_mode(XuLink *link, int timeout_s) {
    const uint32_t magic = rmsl_ctl::kAdbModeMagic;
    std::vector<uint8_t> payload(reinterpret_cast<const uint8_t *>(&magic),
                                 reinterpret_cast<const uint8_t *>(&magic) + sizeof(magic));
    std::vector<uint8_t> resp;
    if (!xu_set_get(link, rmsl_ctl::kCtlCmdAdbMode, payload, &resp)) {
        /* The module reboots immediately on a valid request, so the GET often
         * races the reboot and comes back empty; only a non-empty response
         * (e.g. "invalid magic") is a real rejection. */
        if (!resp.empty()) {
            std::cerr << "adb-mode denied: " << resp_text(resp) << "\n";
            return 1;
        }
    }
    std::cout << "ADB mode cmd sent via XU; waiting for adb device (" << timeout_s
              << "s)...\n";
    const auto t0 = Clock::now();
    while (Clock::now() - t0 < std::chrono::seconds(timeout_s)) {
        std::string out;
        FILE *p = ::popen("adb devices 2>/dev/null", "r");
        if (p) {
            char buf[512];
            while (std::fgets(buf, sizeof(buf), p))
                out += buf;
            ::pclose(p);
        }
        bool found = false;
        std::istringstream is(out);
        std::string serial, state;
        while (is >> serial >> state) {
            if (serial == "List" || serial == "*" || serial.rfind("emulator-", 0) == 0)
                continue;
            if (state == "device") {
                found = true;
                break;
            }
        }
        if (found) {
            std::cout << "  adb device ready:\n" << out;
            return 0;
        }
        std::this_thread::sleep_for(std::chrono::seconds(2));
    }
    std::cerr << "adb-mode: module adb device did not appear within " << timeout_s << "s\n";
    return 1;
}

int cmd_get_calib(XuLink *link, const std::string &dir) {
    constexpr const char *kCalibDir = "/calibration/calib_data";
    constexpr const char *kFiles[] = {"rmsl_fov_config.yaml", "calib_param_1280x1080.yaml"};
    int rc = 0;
    for (auto *f : kFiles) {
        const std::string remote = std::string(kCalibDir) + "/" + f;
        const std::string local = dir.empty() ? f : dir + "/" + f;
        std::cout << "get-calib: " << remote << " -> " << local << "\n";
        if (cmd_file_get(link, remote, local))
            rc = 1;
    }
    return rc;
}

int cmd_set_calib(XuLink *link, const std::string &local) {
    constexpr const char *kCalibDir = "/calibration/calib_data";
    const size_t slash = local.find_last_of('/');
    const std::string base = slash == std::string::npos ? local : local.substr(slash + 1);
    if (base.empty() || base == "." || base == ".." ||
        base.find_first_of(" \t\r\n") != std::string::npos) {
        std::cerr << "set-calib: invalid filename '" << base << "'\n";
        return 1;
    }
    const std::string remote = std::string(kCalibDir) + "/" + base;
    std::cout << "set-calib: " << local << " -> " << remote << "\n";
    return cmd_file_put(link, local, remote);
}

int cmd_set_usb_profile(XuLink *link, const std::string &s) {
    int v = -1;
    if (s == "0" || s == "low")
        v = 0;
    else if (s == "1" || s == "medium")
        v = 1;
    else if (s == "2" || s == "high")
        v = 2;
    else {
        std::cerr << "invalid usb profile: " << s << " (use 0|low, 1|medium, 2|high)\n";
        return 1;
    }
    const int32_t val = static_cast<int32_t>(v);
    std::vector<uint8_t> payload(reinterpret_cast<const uint8_t *>(&val),
                                 reinterpret_cast<const uint8_t *>(&val) + sizeof(val));
    return cmd_control(link, rmsl_ctl::kCtlCmdUsbProfile, payload);
}

int cmd_get_rotate(XuLink *link) {
    std::vector<uint8_t> resp;
    if (!xu_get(link, rmsl_ctl::kCtlCmdRotateMode, &resp)) {
        std::cerr << "get-rotate failed\n";
        return 1;
    }
    int32_t val = 0;
    if (resp.size() >= sizeof(val))
        std::memcpy(&val, resp.data(), sizeof(val));
    std::cout << "rotate: " << (val ? "1 (rotated)" : "0 (normal)") << "\n";
    return 0;
}

int cmd_get_btnr(XuLink *link, uint32_t cmd, const char *sensor) {
    /* BTNR GET is served from the live ISP state by struct_light_app.  Do not
     * send it to the minimal ctl fallback: that path has no ISP context and
     * does not implement this runtime GET. */
    const std::string app = xu_text(link, rmsl_ctl::kCmdGetAppStatus);
    if (app.find("app: running") == std::string::npos) {
        std::cerr << "get-" << sensor
                  << "-btnr requires struct_light_app running and streams open\n";
        return 1;
    }

    std::vector<uint8_t> resp;
    if (!xu_get(link, cmd, &resp)) {
        std::cerr << "get-" << sensor << "-btnr failed\n";
        return 1;
    }
    if (resp.size() < sizeof(int32_t)) {
        std::cerr << "get-" << sensor << "-btnr: short response (" << resp.size()
                  << " bytes)\n";
        return 1;
    }

    int32_t val = -1;
    std::memcpy(&val, resp.data(), sizeof(val));
    if (val != 0 && val != 1) {
        std::cerr << "get-" << sensor
                  << "-btnr unavailable (ISP not initialized; open streams first)\n";
        return 1;
    }
    std::cout << sensor << "-btnr: " << val << "\n";
    return 0;
}

int cmd_set_rotate(XuLink *link, const std::string &s) {
    int v = -1;
    if (s == "0" || s == "off" || s == "normal")
        v = 0;
    else if (s == "1" || s == "on" || s == "rotated")
        v = 1;
    else {
        std::cerr << "invalid rotate: " << s << " (use 0|1)\n";
        return 1;
    }
    const int32_t val = static_cast<int32_t>(v);
    std::vector<uint8_t> payload(reinterpret_cast<const uint8_t *>(&val),
                                 reinterpret_cast<const uint8_t *>(&val) + sizeof(val));
    return cmd_control(link, rmsl_ctl::kCtlCmdRotateMode, payload);
}

/* Device mode (usb_mode) is the single mode source of truth; the removed
 * set-calib-mode wrote a separate calib_mode param that is now gone. */
int cmd_get_device_mode(XuLink *link) {
    std::vector<uint8_t> resp;
    if (!xu_get(link, rmsl_ctl::kCmdGetDeviceMode, &resp)) {
        std::cerr << "get-device-mode failed\n";
        return 1;
    }
    std::cout.write(reinterpret_cast<const char *>(resp.data()), (std::streamsize)resp.size());
    if (!resp.empty() && resp.back() != '\n')
        std::cout << '\n';
    return 0;
}

int cmd_set_device_mode(XuLink *link, const std::string &target) {
    std::vector<uint8_t> payload(target.begin(), target.end());
    std::vector<uint8_t> resp;
    if (!xu_set_get(link, rmsl_ctl::kCmdSetDeviceMode, payload, &resp)) {
        std::cerr << "set-device-mode failed: " << resp_text(resp) << "\n";
        return 1;
    }
    std::cout << "device mode set to " << target << ", module rebooting\n";
    return 0;
}

int cmd_set_projector(XuLink *link, const std::string &s) {
    int v = -1;
    if (s == "1" || s == "on" || s == "speckle")
        v = 1;
    else if (s == "0" || s == "off")
        v = 0; /* device mailbox: 0=off 1=on 2=idle */
    else {
        std::cerr << "invalid projector: " << s << " (use on|off)\n";
        return 1;
    }
    const int32_t val = static_cast<int32_t>(v);
    std::vector<uint8_t> payload(reinterpret_cast<const uint8_t *>(&val),
                                 reinterpret_cast<const uint8_t *>(&val) + sizeof(val));
    return cmd_control(link, rmsl_ctl::kCtlCmdProOnOff, payload);
}

int cmd_set_pro_current(XuLink *link, const std::string &s) {
    int v = std::atoi(s.c_str());
    if (v < 100 || v >= 2000) {
        std::cerr << "invalid projector current: " << s << " (use 100-1900 mA)\n";
        return 1;
    }
    const int32_t val = static_cast<int32_t>(v);
    std::vector<uint8_t> payload(reinterpret_cast<const uint8_t *>(&val),
                                 reinterpret_cast<const uint8_t *>(&val) + sizeof(val));
    return cmd_control(link, rmsl_ctl::kCtlCmdProCurrent, payload);
}

void print_usage() {
    std::cerr <<
        "usage: uvc_host_ctl <subcommand> [args]\n"
        "       uvc_host_ctl --check              control channel quick check\n"
        "\n"
        "control subcommands (over UVC XU):\n"
        "  uvc_host_ctl ping [N]              RTT test (default 3)\n"
        "  uvc_host_ctl get-version           read control version\n"
        "  uvc_host_ctl get-sn                read module SN\n"
        "  uvc_host_ctl file-list DIR         list device directory\n"
        "  uvc_host_ctl file-get PATH [OUT]   download small file\n"
        "  uvc_host_ctl file-put LOCAL REMOTE upload small file\n"
        "  uvc_host_ctl get-param             dump param module conf (debug)\n"
        "  uvc_host_ctl set-algo-mode WxH     set algo mode in ini + reboot module\n"
        "  uvc_host_ctl get-algo-mode         query algo mode / switch status\n"
        "  uvc_host_ctl get-device-mode       query device mode (rgb+depth|rgb+ir|rgb+depth+ir)\n"
        "  uvc_host_ctl set-device-mode M     set device mode (rgb+depth|rgb+ir|rgb+depth+ir) + reboot\n"
        "  uvc_host_ctl get-rotate            read module rotate flag (0|1)\n"
        "  uvc_host_ctl set-rotate 0|1        set module rotate flag\n"
        "  uvc_host_ctl set-projector on|off  speckle projector on/off\n"
        "  uvc_host_ctl set-pro-current N     speckle projector current mA (100-1900)\n"
        "  uvc_host_ctl restart-app           restart struct_light_app only (calib reload)\n"
        "  uvc_host_ctl get-log               fetch module log tail (debug)\n"
        "  uvc_host_ctl clean-log             clear archived + truncate module log\n"
        "  uvc_host_ctl app-status            app process liveness\n"
        "  uvc_host_ctl sensor-status         v4l2 SC132GS sensor presence\n"
        "  uvc_host_ctl vicap-status          dump /proc/rkcif* nodes\n"
        "  uvc_host_ctl isp-status            dump /proc/rkisp* nodes\n"
        "  uvc_host_ctl imu-status            imu app + IIO icm42607 status\n"
        "  uvc_host_ctl health                one-shot production health summary\n"
        "  uvc_host_ctl reboot               reboot module (UVC mode)\n"
        "  uvc_host_ctl reboot-loader        reboot into loader (DFU ready)\n"
        "  uvc_host_ctl adb-mode [SEC]       switch to ADB (magic-gated) + wait for adb device\n"
        "  uvc_host_ctl enter-dfu PART       enter DFU for a partition (e.g. boot_b)\n"
        "  uvc_host_ctl set-usb-profile 0|1|2|low|medium|high   set USB bandwidth profile\n"
        "  uvc_host_ctl get-calib [DIR]      download calib files (default ./)\n"
        "  uvc_host_ctl set-calib FILE       upload a calib file to /calibration/calib_data\n"
        "  uvc_host_ctl get-sync-role        query sync role (master|slave|unknown)\n"
        "  uvc_host_ctl get-cpu-serial       read SoC CPU serial (efuse id)\n"
        "  uvc_host_ctl set-ft-aging -2|-1|0|1   write aging result flag\n"
        "  uvc_host_ctl get-fov              current FOV (float)\n"
        "  uvc_host_ctl get-fov-list         FOV list (raw bytes)\n"
        "  uvc_host_ctl get-fov-crop         current crop (e.g. 640x352)\n"
        "  uvc_host_ctl set-fov <deg>        set FOV (float, immediate + persisted)\n"
        "  uvc_host_ctl set-sn <SN>          write module SN (calib yaml)\n"
        "  uvc_host_ctl set-algo-async|iter|fps|model-type|denoise|full-size <v>\n"
        "  uvc_host_ctl set-algo-uncertain|discontinue|uncertain-delta <v>\n"
        "  uvc_host_ctl get-ir-btnr|get-rgb-btnr       read live IR/RGB BTNR (streams required)\n"
        "  uvc_host_ctl set-ir-btnr|set-rgb-btnr <v>   IR/RGB BTNR\n"
        "  uvc_host_ctl factory-reset        reset module params to defaults\n"
        "  uvc_host_ctl time-sync <us>       sync module clock (8B host us)\n";
}

int cmd_check(XuLink *link, std::string *summary) {
    double sum = 0;
    double best = 1e9;
    for (int i = 0; i < 3; ++i) {
        double rtt = 0;
        if (!xu_ping(link, &rtt)) {
            if (summary)
                *summary = "ping failed";
            return 1;
        }
        sum += rtt;
        best = std::min(best, rtt);
    }

    std::vector<uint8_t> resp;
    if (!xu_get(link, rmsl_ctl::kCmdGetVersion, &resp)) {
        if (summary)
            *summary = "get-version failed";
        return 1;
    }
    const std::string version = resp_text(resp);
    if (!xu_get(link, rmsl_ctl::kCmdGetSn, &resp)) {
        if (summary)
            *summary = "get-sn failed";
        return 1;
    }
    const std::string sn = resp_text(resp);

    std::cout << "  ctl node: " << link->video << "\n"
              << "  ctl ping avg=" << std::fixed << std::setprecision(2) << sum / 3.0
              << " ms (min " << best << " ms)\n"
              << "  ctl version: " << version << "\n"
              << "  ctl sn:      " << sn << "\n";
    if (summary)
        *summary = "node=" + link->video + " rtt=" + std::to_string((int)best) + "ms sn=" + sn;
    return 0;
}

int ctl_cli(XuLink *link, const std::vector<std::string> &args) {
    if (args.empty()) {
        print_usage();
        return 1;
    }
    const std::string &sub = args[0];

    if (sub == "ping") {
        int count = 3;
        if (args.size() >= 2)
            count = std::atoi(args[1].c_str());
        return cmd_ping(link, count);
    }
    if (sub == "get-version")
        return cmd_get(link, rmsl_ctl::kCmdGetVersion);
    if (sub == "get-sn")
        return cmd_get(link, rmsl_ctl::kCmdGetSn);
    if (sub == "file-list" && args.size() >= 2)
        return cmd_file_list(link, args[1]);
    if (sub == "file-get" && args.size() >= 2) {
        const std::string out = args.size() >= 3 ? args[2] : "";
        return cmd_file_get(link, args[1], out);
    }
    if (sub == "file-put" && args.size() >= 3)
        return cmd_file_put(link, args[1], args[2]);
    if (sub == "get-param")
        return cmd_get_param(link);
    if (sub == "get-algo-mode")
        return cmd_get_algo_mode(link);
    if (sub == "set-algo-mode" && args.size() >= 2)
        return cmd_set_algo_mode(link, args[1]);
    if (sub == "get-device-mode")
        return cmd_get_device_mode(link);
    if (sub == "set-device-mode" && args.size() >= 2)
        return cmd_set_device_mode(link, args[1]);
    if (sub == "get-rotate")
        return cmd_get_rotate(link);
    if (sub == "set-rotate" && args.size() >= 2)
        return cmd_set_rotate(link, args[1]);
    if (sub == "set-projector" && args.size() >= 2)
        return cmd_set_projector(link, args[1]);
    if (sub == "set-pro-current" && args.size() >= 2)
        return cmd_set_pro_current(link, args[1]);
    if (sub == "restart-app")
        return cmd_control(link, rmsl_ctl::kCmdRestartApp);
    if (sub == "get-log")
        return cmd_get_log(link);
    if (sub == "clean-log")
        return cmd_clean_log(link);
    if (sub == "app-status")
        return cmd_status(link, rmsl_ctl::kCmdGetAppStatus);
    if (sub == "sensor-status")
        return cmd_status(link, rmsl_ctl::kCmdGetSensorStatus);
    if (sub == "vicap-status")
        return cmd_status(link, rmsl_ctl::kCmdGetVicapStatus);
    if (sub == "isp-status")
        return cmd_status(link, rmsl_ctl::kCmdGetIspStatus);
    if (sub == "imu-status")
        return cmd_status(link, rmsl_ctl::kCmdGetImuStatus);
    if (sub == "health")
        return cmd_health(link);
    if (sub == "reboot")
        return cmd_control(link, rmsl_ctl::kCtlCmdReboot);
    if (sub == "reboot-loader")
        return cmd_control(link, rmsl_ctl::kCtlCmdRebootLoader);
    if (sub == "adb-mode") {
        int sec = 60;
        if (args.size() >= 2)
            sec = std::atoi(args[1].c_str());
        return cmd_adb_mode(link, sec);
    }
    if (sub == "enter-dfu" && args.size() >= 2) {
        std::vector<uint8_t> payload(args[1].begin(), args[1].end());
        payload.push_back('\0');
        return cmd_control(link, rmsl_ctl::kCtlCmdEnterDfu, payload);
    }
    if (sub == "set-usb-profile" && args.size() >= 2)
        return cmd_set_usb_profile(link, args[1]);
    if (sub == "get-sync-role")
        return cmd_get(link, rmsl_ctl::kCmdGetSyncRole);
    if (sub == "get-cpu-serial")
        return cmd_get(link, rmsl_ctl::kCmdGetCpuSerial);
    if (sub == "set-ft-aging" && args.size() >= 2)
        return cmd_set_ft_aging(link, args[1]);
    if (sub == "get-fov")
        return cmd_get_fov_current(link);
    if (sub == "get-fov-list")
        return cmd_get_fov_list(link);
    if (sub == "get-fov-crop")
        return cmd_get_fov_crop(link);
    if (sub == "set-fov" && args.size() >= 2)
        return cmd_set_float(link, rmsl_ctl::kCtlCmdSetFov,
                             static_cast<float>(std::atof(args[1].c_str())));
    if (sub == "set-sn" && args.size() >= 2)
        return cmd_set_str(link, rmsl_ctl::kCtlCmdSetSn, args[1]);
    if (sub == "set-algo-async" && args.size() >= 2)
        return cmd_set_int(link, rmsl_ctl::kCtlCmdAlgoAsyncMode, std::atoll(args[1].c_str()));
    if (sub == "set-algo-iter" && args.size() >= 2)
        return cmd_set_int(link, rmsl_ctl::kCtlCmdAlgoIterCnt, std::atoll(args[1].c_str()));
    if (sub == "set-algo-fps" && args.size() >= 2)
        return cmd_set_int(link, rmsl_ctl::kCtlCmdAlgoFps, std::atoll(args[1].c_str()));
    if (sub == "set-algo-model-type" && args.size() >= 2)
        return cmd_set_int(link, rmsl_ctl::kCtlCmdAlgoModelType, std::atoll(args[1].c_str()));
    if (sub == "set-algo-denoise" && args.size() >= 2)
        return cmd_set_int(link, rmsl_ctl::kCtlCmdAlgoDenoise, std::atoll(args[1].c_str()));
    if (sub == "set-algo-full-size" && args.size() >= 2)
        return cmd_set_int(link, rmsl_ctl::kCtlCmdAlgoFullSize, std::atoll(args[1].c_str()));
    if (sub == "set-algo-uncertain" && args.size() >= 2)
        return cmd_set_int(link, rmsl_ctl::kCtlCmdAlgoUncertain, std::atoll(args[1].c_str()));
    if (sub == "set-algo-discontinue" && args.size() >= 2)
        return cmd_set_int(link, rmsl_ctl::kCtlCmdAlgoDiscontinue, std::atoll(args[1].c_str()));
    if (sub == "set-algo-uncertain-delta" && args.size() >= 2)
        return cmd_set_int(link, rmsl_ctl::kCtlCmdAlgoUncertainDelta, std::atoll(args[1].c_str()));
    if (sub == "get-ir-btnr")
        return cmd_get_btnr(link, rmsl_ctl::kCtlCmdIrBtnr, "ir");
    if (sub == "get-rgb-btnr")
        return cmd_get_btnr(link, rmsl_ctl::kCtlCmdRgbBtnr, "rgb");
    if (sub == "set-ir-btnr" && args.size() >= 2)
        return cmd_set_int(link, rmsl_ctl::kCtlCmdIrBtnr, std::atoll(args[1].c_str()));
    if (sub == "set-rgb-btnr" && args.size() >= 2)
        return cmd_set_int(link, rmsl_ctl::kCtlCmdRgbBtnr, std::atoll(args[1].c_str()));
    if (sub == "factory-reset")
        return cmd_control(link, rmsl_ctl::kCtlCmdFactoryReset);
    if (sub == "time-sync" && args.size() >= 2)
        return cmd_time_sync(link, args[1]);
    if (sub == "get-calib") {
        const std::string dir = args.size() >= 2 ? args[1] : "";
        return cmd_get_calib(link, dir);
    }
    if (sub == "set-calib" && args.size() >= 2)
        return cmd_set_calib(link, args[1]);

    print_usage();
    return 1;
}

} // namespace

int main(int argc, char **argv) {
    bool check_mode = false;
    std::string device;
    std::vector<std::string> args;
    for (int i = 1; i < argc; ++i) {
        const std::string a = argv[i];
        if ((a == "--device" || a == "--video") && i + 1 < argc) {
            device = argv[++i];
        } else if (a == "--check" || a == "--ctl-check" || a == "--ctl-only") {
            check_mode = true;
        } else {
            args.push_back(a);
        }
    }

    XuLink link;
    std::string err;
    if (!xu_open(&link, &err, device)) {
        std::cerr << err << "\n";
        return 1;
    }

    if (check_mode && args.empty()) {
        std::string summary;
        const bool ok = cmd_check(&link, &summary) == 0;
        std::cout << "CTL CHECK: " << (ok ? "PASS" : "FAIL") << " (" << summary << ")\n";
        xu_close(&link);
        return ok ? 0 : 1;
    }
    if (args.empty()) {
        xu_close(&link);
        print_usage();
        return 1;
    }
    const int rc = ctl_cli(&link, args);
    xu_close(&link);
    return rc;
}
