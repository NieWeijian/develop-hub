/*
 * Shared UVC XU (extension unit) transport for host tools.
 *
 * The RMSL control channel is a UVC extension unit (unit 6, selector 1).
 * Every control request is a SET_CUR on selector 1 followed by a GET_CUR.
 * Small responses are read back from selector 1 in 60-byte chunks; large
 * downloads (files, logs, status dumps) are read from selector 2 in
 * 1024-byte chunks.  Chunked responses carry a 2-byte little-endian length
 * prefix so binary payloads (calibration files, logs) are unambiguous:
 *
 *   chunk = [len u16][data up to 58 bytes on selector 1, 1022 on selector 2]
 *
 * A length of 0 marks the end of the response.  The chunk index for
 * multi-chunk reads is encoded in the upper 16 bits of the command id (see
 * rmsl_ctl::kChunkShift on the device side); every chunk select re-sends the
 * command-specific select payload (e.g. the file path) so the module can
 * prepare that chunk on demand.
 */
#pragma once

#include "ctl_proto.h"
#include "xu_trace.hpp"

#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif

#include <cerrno>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <fcntl.h>
#include <limits.h>
#include <linux/usb/ch9.h>
#include <linux/usb/video.h>
#include <linux/usbdevice_fs.h>
#include <linux/uvcvideo.h>
#include <linux/videodev2.h>
#include <string>
#include <sys/ioctl.h>
#include <unistd.h>
#include <vector>

namespace rmsl_xu {

constexpr uint8_t kUnit = 6;        /* UVC extension unit id */
constexpr uint8_t kSelector = 0x01; /* control channel selector (60B) */
constexpr uint8_t kSelectorData = 0x02; /* data channel selector (1024B) */
constexpr uint16_t kDefaultLen = 60;
constexpr uint16_t kLargeLen = 1024;
constexpr uint16_t kMaxLen = kLargeLen;
constexpr uint16_t kCtrlPayload = kDefaultLen - 4; /* max SET payload on ctrl */
constexpr uint16_t kErrorFlag = 0x8000; /* set in the len prefix on errors */
constexpr int32_t kQueryVal = static_cast<int32_t>(0xFFFFFFFFu);

/* Optional fallback: direct usbfs access when the uvcvideo ioctl path is
 * busy (e.g. while the interface is claimed for streaming). */
struct UsbUvc {
    int fd = -1;
    uint8_t ctrl_ifnum = 0;
    bool claimed = false;
};

inline int read_sysfs_uint(const char *path, unsigned *out) {
    FILE *f = std::fopen(path, "r");
    if (!f)
        return -1;
    unsigned v = 0;
    if (std::fscanf(f, "%u", &v) != 1) {
        std::fclose(f);
        return -1;
    }
    std::fclose(f);
    *out = v;
    return 0;
}

/* Open /dev/bus/usb; claim the VC interface only (never DISCONNECT — that
 * kills active streams). */
inline bool usb_uvc_open_for_video(const char *video_dev, UsbUvc *usb) {
    char name[32] = {};
    char sysfs_dev[PATH_MAX] = {};
    char stream_iface[PATH_MAX] = {};
    char ctrl_iface[PATH_MAX] = {};

    if (std::sscanf(video_dev, "/dev/%31s", name) != 1)
        return false;

    std::snprintf(sysfs_dev, sizeof(sysfs_dev), "/sys/class/video4linux/%s/device", name);
    if (!::realpath(sysfs_dev, stream_iface))
        return false;

    std::snprintf(ctrl_iface, sizeof(ctrl_iface), "%s", stream_iface);
    char *colon = std::strrchr(ctrl_iface, ':');
    if (!colon || std::strlen(colon) < 4)
        return false;
    colon[3] = '0';

    unsigned bus = 0;
    unsigned devnum = 0;
    unsigned ifnum = 0;
    char walk[PATH_MAX] = {};
    std::snprintf(walk, sizeof(walk), "%s", ctrl_iface);

    for (int depth = 0; depth < 12; ++depth) {
        char p[PATH_MAX] = {};
        std::snprintf(p, sizeof(p), "%s/busnum", walk);
        if (::access(p, R_OK) != 0) {
            char *slash = std::strrchr(walk, '/');
            if (!slash || slash == walk)
                return false;
            *slash = '\0';
            continue;
        }
        if (read_sysfs_uint(p, &bus) != 0)
            return false;
        std::snprintf(p, sizeof(p), "%s/devnum", walk);
        if (read_sysfs_uint(p, &devnum) != 0)
            return false;
        std::snprintf(p, sizeof(p), "%s/bInterfaceNumber", ctrl_iface);
        if (read_sysfs_uint(p, &ifnum) != 0)
            return false;
        break;
    }

    if (!bus || !devnum)
        return false;

    char usb_path[64] = {};
    std::snprintf(usb_path, sizeof(usb_path), "/dev/bus/usb/%03u/%03u", bus, devnum);
    usb->fd = ::open(usb_path, O_RDWR | O_CLOEXEC);
    if (usb->fd < 0)
        return false;

    usb->ctrl_ifnum = static_cast<uint8_t>(ifnum);
    unsigned ifn = ifnum;
    if (::ioctl(usb->fd, USBDEVFS_CLAIMINTERFACE, &ifn) != 0) {
        /* uvcvideo holds VC while streaming; still try SET_CUR without claim */
        usb->claimed = false;
    } else {
        usb->claimed = true;
    }
    return true;
}

inline void usb_uvc_close(UsbUvc *usb) {
    if (usb->fd >= 0 && usb->claimed) {
        unsigned ifn = usb->ctrl_ifnum;
        (void)::ioctl(usb->fd, USBDEVFS_RELEASEINTERFACE, &ifn);
        usb->claimed = false;
    }
    if (usb->fd >= 0)
        (void)::close(usb->fd);
    usb->fd = -1;
}

inline int usb_uvc_set_cur(UsbUvc const *usb, uint8_t unit, uint8_t selector,
                           const void *data, uint16_t size) {
    if (usb->fd < 0 || !data || !size)
        return -1;
    struct usbdevfs_ctrltransfer ctrl {};
    ctrl.bRequestType = USB_DIR_OUT | USB_TYPE_CLASS | USB_RECIP_INTERFACE;
    ctrl.bRequest = UVC_SET_CUR;
    ctrl.wValue = static_cast<uint16_t>(selector << 8);
    ctrl.wIndex = static_cast<uint16_t>((unit << 8) | usb->ctrl_ifnum);
    ctrl.wLength = size;
    ctrl.timeout = 1000;
    ctrl.data = const_cast<unsigned char *>(static_cast<const unsigned char *>(data));
    return traced_ioctl(usb->fd, USBDEVFS_CONTROL, &ctrl, unit, selector, ctrl.bRequest, ctrl.data, size, true);
}

inline int usb_uvc_get_cur(UsbUvc const *usb, uint8_t unit, uint8_t selector,
                           void *data, uint16_t size) {
    if (usb->fd < 0 || !data || !size)
        return -1;
    struct usbdevfs_ctrltransfer ctrl {};
    ctrl.bRequestType = USB_DIR_IN | USB_TYPE_CLASS | USB_RECIP_INTERFACE;
    ctrl.bRequest = UVC_GET_CUR;
    ctrl.wValue = static_cast<uint16_t>(selector << 8);
    ctrl.wIndex = static_cast<uint16_t>((unit << 8) | usb->ctrl_ifnum);
    ctrl.wLength = size;
    ctrl.timeout = 1000;
    ctrl.data = static_cast<unsigned char *>(data);
    return traced_ioctl(usb->fd, USBDEVFS_CONTROL, &ctrl, unit, selector, ctrl.bRequest, ctrl.data, size, true);
}

/* UVC SET_CUR via uvcvideo on the open video node. */
inline int uvc_set_cur(int fd, uint8_t unit, uint8_t sel, const void *data, uint16_t sz) {
    if (fd < 0 || !data || !sz || sz > kMaxLen)
        return -1;
    uint8_t buf[kMaxLen] = {};
    std::memcpy(buf, data, sz);
    uvc_xu_control_query q{};
    q.unit = unit;
    q.selector = sel;
    q.query = UVC_SET_CUR;
    q.size = sz;
    q.data = buf;
    return traced_ioctl(fd, UVCIOC_CTRL_QUERY, &q, unit, sel, q.query, q.data, sz);
}

inline int uvc_get_cur(int fd, uint8_t unit, uint8_t sel, void *data, uint16_t sz) {
    if (fd < 0 || !data || !sz || sz > kMaxLen)
        return -1;
    uvc_xu_control_query q{};
    q.unit = unit;
    q.selector = sel;
    q.query = UVC_GET_CUR;
    q.size = sz;
    q.data = static_cast<uint8_t *>(data);
    return traced_ioctl(fd, UVCIOC_CTRL_QUERY, &q, unit, sel, q.query, q.data, sz);
}

inline uint16_t uvc_xu_get_len(int fd, uint8_t unit, uint8_t sel) {
    uint8_t buf[2] = {};
    uvc_xu_control_query q{};
    q.unit = unit;
    q.selector = sel;
    q.query = UVC_GET_LEN;
    q.size = sizeof(buf);
    q.data = buf;
    if (traced_ioctl(fd, UVCIOC_CTRL_QUERY, &q, unit, sel, q.query, q.data, sizeof(buf)) != 0)
        return kDefaultLen;
    return static_cast<uint16_t>(buf[0] | (static_cast<unsigned>(buf[1]) << 8));
}

/* uvcvideo requires SET/GET size == UVC_GET_LEN exactly (else ENOBUFS). */
inline bool xu_query_len(int fd, uint8_t sel, uint16_t *len_out) {
    if (fd < 0)
        return false;
    uint16_t len = uvc_xu_get_len(fd, kUnit, sel);
    if (len == 0 || len > kMaxLen)
        len = kDefaultLen;
    if (len_out)
        *len_out = len;
    return true;
}

/* Send one SET_CUR. `payload` is the full request data (command id first).
 * `usb` is an optional usbfs fallback (may be null).  A non-zero `xu_len`
 * reuses a previously negotiated transfer size instead of re-querying
 * GET_LEN on the wire. */
inline bool xu_set_padded(int fd, UsbUvc const *usb, const void *payload, uint16_t payload_sz,
                          uint16_t xu_len = 0) {
    if (!xu_len && !xu_query_len(fd, kSelector, &xu_len))
        return false;
    if (payload_sz > xu_len)
        return false;

    uint8_t buf[kMaxLen] = {};
    if (payload && payload_sz)
        std::memcpy(buf, payload, payload_sz);

    if (uvc_set_cur(fd, kUnit, kSelector, buf, xu_len) == 0)
        return true;

    int err = errno;
    if (usb && usb->fd >= 0 &&
        usb_uvc_set_cur(usb, kUnit, kSelector, buf, xu_len) == 0)
        return true;
    errno = err;
    return false;
}

/* Read one GET_CUR response from `sel` (60 or 1024 bytes raw). `usb` is an
 * optional usbfs fallback.  A non-zero `xu_len` skips the GET_LEN query. */
inline bool xu_get_padded(int fd, UsbUvc const *usb, uint8_t sel, void *out, uint16_t out_sz,
                          uint16_t xu_len = 0) {
    if ((!xu_len && !xu_query_len(fd, sel, &xu_len)) || out_sz > xu_len)
        return false;

    uint8_t buf[kMaxLen] = {};
    if (uvc_get_cur(fd, kUnit, sel, buf, xu_len) == 0) {
        std::memcpy(out, buf, out_sz);
        return true;
    }

    int err = errno;
    if (usb && usb->fd >= 0 &&
        usb_uvc_get_cur(usb, kUnit, sel, buf, xu_len) == 0) {
        std::memcpy(out, buf, out_sz);
        return true;
    }
    errno = err;
    return false;
}

/* Chunk id: base command with the chunk index in bits [16..31]. */
inline uint32_t chunk_cmd(uint32_t base, uint32_t chunk) {
    return base | (chunk << 16);
}

/* Read one length-prefixed chunk. Sets `eof` when the module reports the
 * end of the response. `xu_len` is the negotiated transfer size. */
inline bool read_chunk(int fd, UsbUvc const *usb, uint32_t cmd, uint8_t sel, uint16_t xu_len,
                       std::vector<uint8_t> *data, bool *eof, bool *error = nullptr) {
    (void)cmd;
    if (xu_len < 2)
        return false;
    uint8_t buf[kMaxLen] = {};
    if (!xu_get_padded(fd, usb, sel, buf, xu_len, xu_len))
        return false;
    uint16_t n = static_cast<uint16_t>(buf[0] | (static_cast<unsigned>(buf[1]) << 8));
    if (error)
        *error = (n & kErrorFlag) != 0;
    n &= static_cast<uint16_t>(~kErrorFlag);
    if (n > xu_len - 2)
        return false;
    data->assign(buf + 2, buf + 2 + n);
    *eof = (n == 0 || n < xu_len - 2);
    return true;
}

/* Build the SET payload for a chunk select. Plain queries use
 * [cmd u32][0xFFFFFFFF u32] (the marker is required so the module's set
 * callback runs); commands with select data (e.g. a file path) send
 * [cmd u32][data...] directly. */
inline std::vector<uint8_t> select_payload(uint32_t cmd,
                                           const std::vector<uint8_t> &extra = {}) {
    std::vector<uint8_t> p;
    const uint8_t *cp = reinterpret_cast<const uint8_t *>(&cmd);
    p.insert(p.end(), cp, cp + 4);
    if (extra.empty()) {
        const uint8_t *qp = reinterpret_cast<const uint8_t *>(&kQueryVal);
        p.insert(p.end(), qp, qp + 4);
    }
    p.insert(p.end(), extra.begin(), extra.end());
    return p;
}

/* One-shot SET command with optional payload (device executes; a response is
 * prepared for the optional following GET). */
inline bool xu_set_cmd(int fd, UsbUvc const *usb, uint32_t cmd,
                       const std::vector<uint8_t> &payload) {
    if (payload.size() > kCtrlPayload)
        return false;
    std::vector<uint8_t> p;
    p.reserve(4 + payload.size());
    const uint8_t *cp = reinterpret_cast<const uint8_t *>(&cmd);
    p.insert(p.end(), cp, cp + 4);
    p.insert(p.end(), payload.begin(), payload.end());
    return xu_set_padded(fd, usb, p.data(), static_cast<uint16_t>(p.size()));
}

/* Read the full response for a command (multi-chunk). `extra` is
 * command-specific select data (e.g. a file path); empty for plain queries.
 * Each chunk is selected with the same extra payload. */
inline bool xu_get_all(int fd,
                       UsbUvc const *usb,
                       uint32_t base_cmd,
                       const std::vector<uint8_t> &extra,
                       std::vector<uint8_t> *out) {
    if (!out)
        return false;
    out->clear();

    uint16_t ctrl_len = 0;
    if (!xu_query_len(fd, kSelector, &ctrl_len))
        return false;

    uint8_t get_sel = rmsl_ctl::is_large_response(base_cmd) ? kSelectorData : kSelector;
    uint16_t data_len = ctrl_len;
    if (get_sel == kSelectorData) {
        if (!xu_query_len(fd, kSelectorData, &data_len) || data_len < kLargeLen) {
            /* Old firmware has no large data channel: fall back to sel 1. */
            get_sel = kSelector;
            data_len = ctrl_len;
        }
    }

    for (uint32_t chunk = 0; chunk < 0xFFFFu; ++chunk) {
        const uint32_t cmd = chunk_cmd(base_cmd, chunk);
        const std::vector<uint8_t> select = select_payload(cmd, extra);
        if (!xu_set_padded(fd, usb, select.data(), static_cast<uint16_t>(select.size()),
                           ctrl_len))
            return false;
        std::vector<uint8_t> part;
        bool eof = false;
        bool error = false;
        if (!read_chunk(fd, usb, cmd, get_sel, data_len, &part, &eof, &error))
            return false;
        if (error)
            return false; /* out holds the error message (if any) */
        out->insert(out->end(), part.begin(), part.end());
        if (eof)
            break;
    }
    return true;
}

/* Convenience: select + GET for commands whose response fits one chunk. */
inline bool xu_get_simple(int fd, UsbUvc const *usb, uint32_t cmd, std::vector<uint8_t> *out) {
    return xu_get_all(fd, usb, cmd, {}, out);
}

} // namespace rmsl_xu
