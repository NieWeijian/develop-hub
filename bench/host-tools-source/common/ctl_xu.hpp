/*
 * Host-side RMSL control channel over UVC XU.
 *
 * Wraps rmsl_xu transport for the ctl/ft/ota tools: opens the RMSL32_RGB
 * video node (the XU control interface) with an optional usbfs fallback and
 * exposes the request helpers used by the control protocol (see
 * src/ctl/ctl_service.h for the device-side contract).
 */
#pragma once

#include "ctl_proto.h"
#include "uvc_device.hpp"
#include "uvc_xu.hpp"
#include "v4l2_capture.hpp"

#include <cerrno>
#include <chrono>
#include <cstdint>
#include <cstring>
#include <fcntl.h>
#include <string>
#include <unistd.h>
#include <vector>

namespace ctl_xu {

struct Link {
    int fd = -1;
    rmsl_xu::UsbUvc usb{};
    std::string video;
};

/* Open the control channel.  `device` (e.g. /dev/videoN) pins the module when
 * several are connected; empty keeps the legacy auto-find behaviour. */
inline bool open(Link *link, std::string *err = nullptr,
                 std::string const &device = {}) {
    std::string dev;
    if (!device.empty()) {
        dev = device;
    } else {
        const auto found =
            uvc_tools::find_capture_device_any({"RMSL321_RGB", "RMSL322_RGB"});
        if (!found) {
            if (err)
                *err = "no RMSL UVC control node found (is the module connected?)";
            return false;
        }
        dev = *found;
    }
    link->fd = ::open(dev.c_str(), O_RDWR | O_CLOEXEC | O_NONBLOCK);
    if (link->fd < 0) {
        if (err)
            *err = "cannot open " + dev + ": " + std::strerror(errno);
        return false;
    }
    link->video = dev;
    (void)rmsl_xu::usb_uvc_open_for_video(dev.c_str(), &link->usb);
    return true;
}

inline void close(Link *link) {
    rmsl_xu::usb_uvc_close(&link->usb);
    if (link->fd >= 0)
        ::close(link->fd);
    link->fd = -1;
}

inline bool get(Link *link, uint32_t cmd, std::vector<uint8_t> *resp) {
    return rmsl_xu::xu_get_all(link->fd, &link->usb, cmd, {}, resp);
}

inline bool get_extra(Link *link,
                      uint32_t cmd,
                      const std::vector<uint8_t> &extra,
                      std::vector<uint8_t> *resp) {
    return rmsl_xu::xu_get_all(link->fd, &link->usb, cmd, extra, resp);
}

/* SET then read the single-chunk response. Returns false on transport error
 * or a device error (resp then holds the error message). */
inline bool set_get(Link *link,
                    uint32_t cmd,
                    const std::vector<uint8_t> &payload,
                    std::vector<uint8_t> *resp) {
    if (!rmsl_xu::xu_set_cmd(link->fd, &link->usb, cmd, payload))
        return false;
    uint16_t ctrl_len = 0;
    if (!rmsl_xu::xu_query_len(link->fd, rmsl_xu::kSelector, &ctrl_len))
        return false;
    std::vector<uint8_t> part;
    bool eof = false;
    bool error = false;
    if (!rmsl_xu::read_chunk(link->fd, &link->usb, cmd, rmsl_xu::kSelector, ctrl_len,
                             &part, &eof, &error))
        return false;
    if (resp)
        *resp = std::move(part);
    return !error;
}

/* Fire-and-forget SET. */
inline bool set(Link *link, uint32_t cmd, const std::vector<uint8_t> &payload = {}) {
    return rmsl_xu::xu_set_cmd(link->fd, &link->usb, cmd, payload);
}

inline bool ping(Link *link, double *rtt_ms) {
    std::vector<uint8_t> resp;
    const auto t0 = std::chrono::steady_clock::now();
    if (!get(link, rmsl_ctl::kCmdPing, &resp))
        return false;
    const auto t1 = std::chrono::steady_clock::now();
    if (rtt_ms)
        *rtt_ms = std::chrono::duration<double, std::milli>(t1 - t0).count();
    return true;
}

inline std::string text(Link *link, uint32_t cmd) {
    std::vector<uint8_t> resp;
    if (!get(link, cmd, &resp))
        return {};
    return std::string(resp.begin(), resp.end());
}

} // namespace ctl_xu
