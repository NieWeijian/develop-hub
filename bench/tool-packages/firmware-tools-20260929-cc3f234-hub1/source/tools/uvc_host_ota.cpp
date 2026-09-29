/*
 * Copyright (C) 2026 Rockchip Electronics Co., Ltd.
 *
 * RMSL321 host-side DFU OTA test tool.
 *
 * Flow:
 *   1. (optional) Ask the module's HID control channel to enter DFU
 *      (kCtlCmdEnterDfu) with a target-slot partition name. The module erases
 *      that partition, sets `dfu_once`, and reboots into U-Boot DFU mode.
 *   2. Wait for the DFU device (2207:0107) and verify the expected alternate
 *      settings (uboot_a/boot_a/system_a or *_b, of the other slot) via dfu-util -l.
 *   3. Flash each partition with `dfu-util -d 2207:0107 -a <alt> -D <img>`
 *      (default set: boot/system/oem), the last one with `-R` to reset back to
 *      runtime mode.
 *   4. Wait for the runtime device (2207:001a, RMSL321 UVC) to come back and
 *      report PASS/FAIL.
 *
 * The DFU alternate set exposed by U-Boot always points to the slot the module
 * is NOT booting from (CONFIG_ANDROID_AB). The current slot is read from the
 * module version string returned over HID (kCmdGetVersion), e.g. "... _a".
 *
 * Build: cmake -B build && cmake --build build
 *
 * Usage:
 *   ./uvc_host_ota                                     # full loop (enter DFU -> flash -> reboot)
 *   ./uvc_host_ota --flash-only                        # module already in DFU mode
 *   ./uvc_host_ota --enter-dfu-only                    # only send ENTER_DFU (scripted setups)
 *   ./uvc_host_ota --verify-only                       # only wait for runtime device + version
 *   ./uvc_host_ota --image-dir ../output/image --parts uboot,boot,system
 *   ./uvc_host_ota --slot b --json
 */

#include "v4l2_capture.hpp"
#include "ctl_proto.h"
#include "ctl_xu.hpp"

#include <cerrno>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <fcntl.h>
#include <iostream>
#include <linux/usb/video.h>
#include <linux/uvcvideo.h>
#include <optional>
#include <string>
#include <string_view>
#include <sys/ioctl.h>
#include <thread>
#include <unistd.h>
#include <vector>

namespace fs = std::filesystem;
using namespace std::chrono_literals;

namespace {

constexpr unsigned k_vid_rockchip = 0x2207;
constexpr unsigned k_pid_runtime = 0x001A;
constexpr unsigned k_pid_dfu = 0x0107;

/* Flash data partitions first and boot LAST: U-Boot verifies the boot
 * partition on the next reset, so boot must only be replaced once system/oem
 * are already in place (a failed/interrupted flash then still boots). */
const char *k_default_parts[] = {"oem", "system", "boot"};

struct Options {
    bool enter_dfu = true;
    bool flash = true;
    bool verify = true;
    bool json = false;
    bool no_reboot = false;
    unsigned timeout_s = 120;
    std::string image_dir = "./output/image";
    std::vector<std::string> parts{k_default_parts[0], k_default_parts[1], k_default_parts[2]};
    std::optional<std::string> slot_override;
    std::string device; /* /dev/videoN pinning the module (multi-module hubs) */
    std::string port;   /* USB port path e.g. "3-1.1" for dfu-util -p */
};

void print_usage() {
    std::cout
        << "Usage: uvc_host_ota [options]\n"
        << "  --enter-dfu-only      send XU ENTER_DFU and exit\n"
        << "  --flash-only          module already in DFU; skip entry\n"
        << "  --verify-only         skip flashing; wait for runtime device and query version\n"
        << "  --image-dir DIR       directory containing <part>.img (default ./output/image)\n"
        << "  --parts a,b,c         partitions to flash (default oem,system,boot)\n"
        << "  --slot a|b            target slot override (default: other than current boot slot)\n"
        << "  --device /dev/videoN  pin the module by its RGB video node\n"
        << "  --port BUS-PORT       pin the module by USB port (e.g. 3-1.1) for dfu-util\n"
        << "  --timeout N           device wait timeout in seconds (default 120)\n"
        << "  --no-reboot           do not pass -R to the last dfu-util invocation\n"
        << "  --json                JSON result output\n";
}

/* --------------------------------------------------------------- XU ---- */

static ctl_xu::Link g_ctl;
static std::string g_device;
static std::string g_port_path;

static bool ctl_ensure_open(bool quiet = false) {
    if (g_ctl.fd >= 0)
        return true;
    std::string err;
    if (!ctl_xu::open(&g_ctl, &err, g_device)) {
        if (!quiet)
            std::cerr << err << "\n";
        return false;
    }
    return true;
}

/* Fire-and-forget SET: the module may reboot before the response is ready
 * (e.g. ENTER_DFU), so the caller verifies by waiting for the DFU device. */
static bool ctl_send_fire(uint32_t cmd, const std::vector<uint8_t> &payload = {}) {
    if (!ctl_ensure_open(true))
        return false;
    return ctl_xu::set(&g_ctl, cmd, payload);
}

static bool ctl_query_string(uint32_t cmd, std::string *out) {
    if (!ctl_ensure_open(true))
        return false;
    std::vector<uint8_t> resp;
    if (!ctl_xu::get(&g_ctl, cmd, &resp))
        return false;
    if (out)
        out->assign(resp.begin(), resp.end());
    return true;
}

/* ----------------------------------------------------------------- USB --- */

static bool read_hex_file(fs::path const &p, unsigned *out) {
    std::ifstream f(p);
    if (!f)
        return false;
    unsigned v = 0;
    if (!(f >> std::hex >> v))
        return false;
    *out = v;
    return true;
}

static bool usb_device_present(unsigned vid, unsigned pid) {
    if (!g_port_path.empty()) {
        fs::path const p = fs::path("/sys/bus/usb/devices") / g_port_path;
        unsigned v = 0, pp = 0;
        return read_hex_file(p / "idVendor", &v) && read_hex_file(p / "idProduct", &pp) &&
               v == vid && pp == pid;
    }
    fs::path const root = "/sys/bus/usb/devices";
    if (!fs::exists(root))
        return false;
    for (auto const &entry : fs::directory_iterator(root)) {
        if (!fs::is_directory(entry))
            continue;
        unsigned v = 0, p = 0;
        if (read_hex_file(entry.path() / "idVendor", &v) &&
            read_hex_file(entry.path() / "idProduct", &p) && v == vid && p == pid)
            return true;
    }
    return false;
}

static bool wait_usb_device(unsigned vid, unsigned pid, unsigned timeout_s) {
    auto const deadline = std::chrono::steady_clock::now() + std::chrono::seconds(timeout_s);
    while (std::chrono::steady_clock::now() < deadline) {
        if (usb_device_present(vid, pid))
            return true;
        std::this_thread::sleep_for(500ms);
    }
    return false;
}

/* ----------------------------------------------------------- dfu-util --- */

static bool dfu_util_available() {
    return ::system("command -v dfu-util >/dev/null 2>&1") == 0;
}

static std::string run_capture(char const *cmd) {
    std::string out;
    FILE *pipe = ::popen(cmd, "r");
    if (!pipe)
        return out;
    char buf[512];
    size_t n = 0;
    while ((n = ::fread(buf, 1, sizeof(buf), pipe)) > 0)
        out.append(buf, n);
    ::pclose(pipe);
    return out;
}

static bool dfu_has_alt(std::string const &dfu_list, std::string const &alt) {
    std::string needle = "name=\"" + alt + "\"";
    return dfu_list.find(needle) != std::string::npos;
}

static int dfu_flash(std::string const &alt, fs::path const &image, bool reboot) {
    /* Long timeout so slow NAND erase/write of big partitions (e.g. oem) fits.
     * Ignored by stock dfu-util; honored by builds that read DFU_TIMEOUT. */
    std::string cmd = "DFU_TIMEOUT=120000 dfu-util -d 2207:0107 -a " + alt + " -D "
                      + image.string();
    if (!g_port_path.empty())
        cmd += " -p " + g_port_path;
    if (reboot)
        cmd += " -R";
    cmd += " >/dev/null 2>&1";
    int ret = ::system(cmd.c_str());
    /* After a successful -R reset the device disappears; dfu-util's
     * libusb_reset_device() returns LIBUSB_ERROR_NOT_FOUND (-5) and main()
     * exits with 251. Treat that as success. */
    if (reboot && ret == (251 << 8))
        return 0;
    return ret;
}

/* ------------------------------------------------------------ module --- */

static bool detect_slot(std::string const &version, std::string *slot) {
    std::string tail = version;
    auto pos = tail.rfind(' ');
    if (pos != std::string::npos)
        tail = tail.substr(pos + 1);
    if (tail == "_a" || tail == "_b" || tail == "A" || tail == "B") {
        *slot = (tail == "_a" || tail == "A") ? "_a" : "_b";
        return true;
    }
    return false;
}

static std::string other_slot(std::string const &slot) {
    return slot == "_a" ? "_b" : "_a";
}

/* Verify every <part>.img exists and is non-empty BEFORE the module is
 * rebooted into DFU: once it is in DFU the runtime side is offline, so a
 * missing image would strand the flash mid-way. */
static bool check_images(std::vector<std::string> const &parts,
                         fs::path const &image_dir,
                         std::string *missing) {
    for (auto const &part : parts) {
        fs::path image = image_dir / (part + ".img");
        std::error_code ec;
        if (!fs::exists(image, ec) || fs::file_size(image, ec) == 0) {
            if (missing)
                *missing = image.string();
            return false;
        }
    }
    return true;
}

static bool run_dfutil(std::vector<std::string> const &parts, std::string const &slot,
                       fs::path const &image_dir, bool reboot) {
    if (!dfu_util_available()) {
        std::cerr << "dfu-util not found in PATH; install it first (e.g. apt install dfu-util)\n";
        return false;
    }

    std::string list =
        run_capture((std::string("dfu-util -d 2207:0107") +
                     (g_port_path.empty() ? "" : " -p " + g_port_path) +
                     " -l 2>/dev/null")
                        .c_str());
    if (list.find("Found DFU: [2207:0107]") == std::string::npos) {
        std::cerr << "DFU device 2207:0107 not found by dfu-util -l\n";
        return false;
    }

    for (auto const &part : parts) {
        std::string alt = part + slot;
        if (!dfu_has_alt(list, alt)) {
            std::cerr << "DFU alternate \"" << alt << "\" not present; listing:\n" << list;
            return false;
        }
        std::cout << "flashing " << alt << " <- " << part << ".img\n";
    }

    bool all_ok = true;
    for (size_t i = 0; i < parts.size(); ++i) {
        std::string alt = parts[i] + slot;
        fs::path image = image_dir / (parts[i] + ".img");
        if (!fs::exists(image)) {
            std::cerr << "image not found: " << image << "\n";
            all_ok = false;
            break;
        }
        bool last = (i + 1 == parts.size());
        int ret = dfu_flash(alt, image, last && reboot);
        if (ret != 0) {
            std::cerr << "dfu-util failed for " << alt << " (exit=" << ret << ")\n";
            all_ok = false;
            break;
        }
        std::cout << "  " << alt << " OK\n";
    }
    return all_ok;
}

/* --------------------------------------------------------------- main --- */

int run(int argc, char **argv) {
    Options opt;
    for (int i = 1; i < argc; ++i) {
        std::string_view a = argv[i];
        auto next = [&](std::string_view name) -> std::string {
            if (i + 1 >= argc) {
                std::cerr << "missing value for " << name << "\n";
                exit(2);
            }
            return argv[++i];
        };
        if (a == "--enter-dfu-only") {
            opt.enter_dfu = true;
            opt.flash = false;
            opt.verify = false;
        } else if (a == "--flash-only") {
            opt.enter_dfu = false;
            opt.flash = true;
            opt.verify = true;
        } else if (a == "--verify-only") {
            opt.enter_dfu = false;
            opt.flash = false;
            opt.verify = true;
        } else if (a == "--image-dir") {
            opt.image_dir = next(a);
        } else if (a == "--parts") {
            std::string parts = next(a);
            opt.parts.clear();
            size_t start = 0;
            while (start <= parts.size()) {
                size_t comma = parts.find(',', start);
                if (comma == std::string::npos)
                    comma = parts.size();
                if (comma > start)
                    opt.parts.push_back(parts.substr(start, comma - start));
                start = comma + 1;
            }
        } else if (a == "--slot") {
            std::string s = next(a);
            if (s == "a" || s == "A")
                opt.slot_override = "_a";
            else if (s == "b" || s == "B")
                opt.slot_override = "_b";
            else {
                std::cerr << "invalid slot: " << s << "\n";
                return 2;
            }
        } else if (a == "--device" || a == "--video") {
            opt.device = next(a);
        } else if (a == "--port") {
            opt.port = next(a);
        } else if (a == "--timeout") {
            opt.timeout_s = static_cast<unsigned>(std::atoi(next(a).c_str()));
        } else if (a == "--no-reboot") {
            opt.no_reboot = true;
        } else if (a == "--json") {
            opt.json = true;
        } else if (a == "-h" || a == "--help") {
            print_usage();
            return 0;
        } else {
            std::cerr << "unknown option: " << a << "\n";
            print_usage();
            return 2;
        }
    }

    g_device = opt.device;
    g_port_path = opt.port;
    if (g_port_path.empty() && !g_device.empty())
        g_port_path = uvc_tools::usb_port_path(g_device);

    struct Phase {
        std::string name;
        bool pass = false;
        std::string detail;
    };
    std::vector<Phase> phases;
    bool overall = true;

    std::string target_slot;
    std::string runtime_dev;

    /* 0. Pre-flight: verify the images before touching the module. */
    if (opt.flash) {
        std::string missing;
        if (!check_images(opt.parts, opt.image_dir, &missing)) {
            if (opt.json)
                std::cout << "{\"overall\":\"FAIL\",\"phases\":[]}\n";
            else
                std::cout << "RESULT: FAIL\n";
            std::cerr << "firmware image missing or empty: " << missing
                      << " (aborted before entering DFU)\n";
            return 1;
        }
    }

    if (opt.slot_override)
        target_slot = *opt.slot_override;

    /* 1. Optional: enter DFU via HID control. */
    if (opt.enter_dfu) {
        Phase ph{"enter-dfu", false, ""};
        std::string version;
        if (!ctl_query_string(rmsl_ctl::kCmdGetVersion, &version)) {
            ph.detail = "control version query failed (module not in runtime mode?)";
        } else {
            std::string cur;
            if (!opt.slot_override) {
                if (!detect_slot(version, &cur)) {
                    ph.detail = "cannot detect boot slot from version: \"" + version + "\"";
                } else {
                    target_slot = other_slot(cur);
                }
            } else {
                target_slot = *opt.slot_override;
            }
            ph.detail = "version=\"" + version + "\"";

            if (ph.detail.rfind("version=", 0) == 0) {
                std::string part_list;
                for (size_t i = 0; i < opt.parts.size(); ++i) {
                    if (i)
                        part_list += ',';
                    part_list += opt.parts[i] + target_slot;
                }
                std::cout << "module -> enter DFU, erase " << part_list << "\n";
                std::vector<uint8_t> payload(part_list.begin(), part_list.end());
                payload.push_back('\0');
                if (ctl_send_fire(rmsl_ctl::kCtlCmdEnterDfu, payload))
                    ph.pass = true;
                else
                    ph.detail += "; ENTER_DFU send failed";
            }
        }
        overall &= ph.pass;
        phases.push_back(std::move(ph));
        if (!overall) {
            if (opt.json) {
                std::cout << "{\"overall\":\"FAIL\",\"phases\":[";
                for (size_t i = 0; i < phases.size(); ++i) {
                    if (i)
                        std::cout << ",";
                    std::cout << "{\"name\":\"" << phases[i].name << "\",\"pass\":"
                              << (phases[i].pass ? "true" : "false") << "}";
                }
                std::cout << "]}\n";
            } else {
                std::cout << "RESULT: FAIL\n";
            }
            return 1;
        }
    }

    /* 2. Wait for DFU device and flash. */
    if (opt.flash) {
        Phase ph{"dfu-flash", false, ""};
        std::cout << "waiting for DFU device 2207:0107 (timeout " << opt.timeout_s << "s)...\n";
        if (!wait_usb_device(k_vid_rockchip, k_pid_dfu, opt.timeout_s)) {
            ph.detail = "DFU device 2207:0107 did not appear";
        } else if (target_slot.empty()) {
            ph.detail = "target slot unknown; use --slot a|b";
        } else if (run_dfutil(opt.parts, target_slot, opt.image_dir, !opt.no_reboot)) {
            ph.pass = true;
            ph.detail = "flashed " + std::to_string(opt.parts.size()) + " partition(s) to slot "
                        + target_slot;
        } else {
            ph.detail = "dfu-util flashing failed";
        }
        overall &= ph.pass;
        phases.push_back(std::move(ph));
    }

    /* 3. Wait for runtime device to come back. */
    if (opt.verify && overall) {
        Phase ph{"runtime-verify", false, ""};
        std::cout << "waiting for runtime device 2207:001a (timeout " << opt.timeout_s << "s)...\n";
        if (!wait_usb_device(k_vid_rockchip, k_pid_runtime, opt.timeout_s)) {
            ph.detail = "runtime device 2207:001a did not reappear";
        } else {
            std::string version;
            std::string slot;
            if (ctl_query_string(rmsl_ctl::kCmdGetVersion, &version))
                detect_slot(version, &slot);
            ph.pass = true;
            ph.detail = "runtime device OK";
            if (!version.empty())
                ph.detail += ", version=\"" + version + "\"";
            else
                ph.detail += " (version query skipped)";
        }
        overall &= ph.pass;
        phases.push_back(std::move(ph));
    }

    if (opt.json) {
        std::cout << "{\"overall\":\"" << (overall ? "PASS" : "FAIL") << "\",\"phases\":[";
        for (size_t i = 0; i < phases.size(); ++i) {
            if (i)
                std::cout << ",";
            std::cout << "{\"name\":\"" << phases[i].name << "\",\"pass\":"
                      << (phases[i].pass ? "true" : "false")
                      << ",\"detail\":\"" << phases[i].detail << "\"}";
        }
        std::cout << "]}\n";
    } else {
        for (auto const &ph : phases)
            std::cout << "[" << (ph.pass ? "PASS" : "FAIL") << "] " << ph.name << ": "
                      << ph.detail << "\n";
        std::cout << "RESULT: " << (overall ? "PASS" : "FAIL") << "\n";
    }
    return overall ? 0 : 1;
}

}  // namespace

int main(int argc, char **argv) {
    try {
        return run(argc, argv);
    } catch (std::exception const &e) {
        std::cerr << "FATAL: " << e.what() << "\n";
        return 1;
    }
}
