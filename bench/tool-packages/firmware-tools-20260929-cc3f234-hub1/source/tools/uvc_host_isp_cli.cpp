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

#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif

#include <array>
#include <atomic>
#include <cerrno>
#include <chrono>
#include <cinttypes>
#include <condition_variable>
#include <cstdarg>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <fcntl.h>
#include <filesystem>
#include <fstream>
#include <limits.h>
#include <linux/usb/ch9.h>
#include <linux/usb/video.h>
#include <linux/usbdevice_fs.h>
#include <linux/uvcvideo.h>
#include <linux/v4l2-controls.h>
#include <linux/videodev2.h>
#include <mutex>
#include <optional>
#include <string>
#include <string_view>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <thread>
#include <unistd.h>
#include <vector>


#include "drm_preview.hpp"
#include "v4l2_capture.hpp"


namespace {

uvc_tools::DrmScreen *g_drm = nullptr;
bool g_drm_enable = true;

constexpr unsigned k_v4l2_nbuffers = 8;
constexpr unsigned k_v4l2_nbuffers_rgb = 12;
constexpr unsigned k_ir_start_delay_ms = 400;
constexpr unsigned k_uvc_ctrl_interval_us = 30000;
constexpr unsigned k_v4l2_ctrl_settle_us = 50000;

constexpr unsigned k_lane_rgb = 0;
constexpr unsigned k_lane_depth = 1;
constexpr unsigned k_lane_ir = 2;

/* Matches uvc-gadget.h UVC_CTRL_*_ID */
constexpr uint8_t k_uvc_unit_ct = 1;
constexpr uint8_t k_uvc_unit_pu = 2;
constexpr uint8_t k_uvc_unit_xu = 6;
constexpr uint8_t k_xu_sel_tools = 0x01;

constexpr uint32_t k_rmsl_cmd_ir_btnr = 0xFF00010Au;
constexpr uint32_t k_rmsl_cmd_rgb_btnr = 0xFF00010Bu;
/** Matches MAX_UVC_REQUEST_DATA_LENGTH in src/uvc/uvc-gadget.h */
constexpr uint16_t k_xu_tools_max_len = 60;
/** GET prelude marker in SET payload[4..7]; must not be 0/1 (valid btnr values). */
constexpr int32_t k_rmsl_xu_query_val = static_cast<int32_t>(0xFFFFFFFFu);

struct CaptureProfile {
    char const *card_match;
    uvc_tools::CaptureFormat format;
};

constexpr CaptureProfile k_profile_rmsl_rgb = {
    "RMSL32_RGB",
    {V4L2_PIX_FMT_NV12, 640, 540, 10},
};

constexpr CaptureProfile k_profile_rmsl_depth = {
    "RMSL32_DEPTH",
    {V4L2_PIX_FMT_YUYV, 640, 352, 10},
};

constexpr CaptureProfile k_profile_rmsl_ir = {
    "RMSL32_IR",
    {V4L2_PIX_FMT_NV12, 1080, 1280, 10},
};

std::mutex g_log_mtx;
std::atomic<bool> g_app_stop{false};

struct StreamSession;
StreamSession *g_rgb_sess = nullptr;
StreamSession *g_depth_sess = nullptr;
StreamSession *g_ir_sess = nullptr;
bool g_ir_controls_available = false;

enum class ActiveCamera {
    RGB,
    IR
};
std::atomic<ActiveCamera> g_active_camera{ActiveCamera::RGB};

static StreamSession *active_camera_sess() {
    return g_active_camera.load() == ActiveCamera::IR ? g_ir_sess : g_rgb_sess;
}

static char const *active_camera_name() {
    return g_active_camera.load() == ActiveCamera::IR ? "ir" : "rgb";
}

static bool is_active_preview(StreamSession const *sess) {
    return sess && sess == active_camera_sess();
}

constexpr unsigned k_ir_eye_h = 1280;
constexpr unsigned k_ir_combine_h = 2560;
constexpr uint16_t k_uvc_head_magic = 0xA55A;
constexpr uint16_t k_uvc_frame_left = 4;
constexpr uint16_t k_uvc_frame_right = 5;

struct IrEyeCache {
    std::vector<uint8_t> nv12;
    bool ready = false;
};

struct IrPairCache {
    IrEyeCache left;
    IrEyeCache right;
    uint32_t w = 0;
    uint32_t h = 0;
};

static IrPairCache g_ir_pair;
static std::mutex g_ir_pair_mtx;

static size_t nv12_bytes(uint32_t w, uint32_t h) {
    return static_cast<size_t>(w) * h * 3 / 2;
}

static void ir_cache_single_frame(void const *ptr, size_t sz, uint32_t w, uint32_t h) {
    if (!ptr || !w || !h || sz < 8)
        return;

    auto const *p = static_cast<uint8_t const *>(ptr);
    uint16_t magic = static_cast<uint16_t>(p[0] | (p[1] << 8));
    if (magic != k_uvc_head_magic)
        return;

    uint16_t type = static_cast<uint16_t>(p[6] | (p[7] << 8));
    size_t need = nv12_bytes(w, h);
    if (sz < need)
        return;

    std::lock_guard<std::mutex> lk(g_ir_pair_mtx);
    g_ir_pair.w = w;
    g_ir_pair.h = h;
    if (type == k_uvc_frame_left) {
        g_ir_pair.left.nv12.assign(p, p + need);
        g_ir_pair.left.ready = true;
    } else if (type == k_uvc_frame_right) {
        g_ir_pair.right.nv12.assign(p, p + need);
        g_ir_pair.right.ready = true;
    }
}

static bool ir_pair_ready(uint32_t *w, uint32_t *h, void const **left, void const **right) {
    std::lock_guard<std::mutex> lk(g_ir_pair_mtx);
    if (!g_ir_pair.left.ready || !g_ir_pair.right.ready || !g_ir_pair.w || !g_ir_pair.h)
        return false;
    *w = g_ir_pair.w;
    *h = g_ir_pair.h;
    *left = g_ir_pair.left.nv12.data();
    *right = g_ir_pair.right.nv12.data();
    return true;
}

static void present_ir_frame(void const *ptr, uint32_t pixfmt, uint32_t w, uint32_t h) {
    if (!g_drm || !ptr || !w || !h)
        return;

    if (h > k_ir_eye_h) {
        unsigned eye_h = h / 2;
        size_t half = nv12_bytes(w, eye_h);
        auto const *base = static_cast<uint8_t const *>(ptr);
        g_drm->present_ir_lr(base, base + half, pixfmt, w, eye_h);
        return;
    }

    ir_cache_single_frame(ptr, nv12_bytes(w, h), w, h);
    void const *left = nullptr;
    void const *right = nullptr;
    uint32_t ew = 0;
    uint32_t eh = 0;
    if (ir_pair_ready(&ew, &eh, &left, &right))
        g_drm->present_ir_lr(left, right, pixfmt, ew, eh);
}

struct FpsCounter {
    std::atomic<uint64_t> frames{0};
    std::mutex snap_mtx;
    std::chrono::steady_clock::time_point t0{};
    std::chrono::steady_clock::time_point t_last{};
    uint64_t frames_at_last = 0;
    bool inited = false;
};

void cam_log(unsigned lane, char const *fmt, ...) {
    va_list ap;
    std::lock_guard<std::mutex> lk(g_log_mtx);
    fprintf(stderr, "[cam%u] ", lane);
    va_start(ap, fmt);
    vfprintf(stderr, fmt, ap);
    va_end(ap);
}

void cam_perror(unsigned lane, char const *msg) {
    int save = errno;
    std::lock_guard<std::mutex> lk(g_log_mtx);
    errno = save;
    fprintf(stderr, "[cam%u] ", lane);
    perror(msg);
}

constexpr std::array<uint32_t, 3> k_pix_fallback{
    V4L2_PIX_FMT_NV12, V4L2_PIX_FMT_YUYV, V4L2_PIX_FMT_MJPEG};

struct StreamSession {
    unsigned lane = 0;
    std::string devpath;
    uvc_tools::V4l2Capture capture;
    std::atomic<bool> stop{false};
    std::atomic<int> worker_ret{0};
    std::thread worker;
    std::mutex fd_mtx;
    FpsCounter capture_fps;
    FpsCounter display_fps;
};

struct UsbUvc {
    int fd = -1;
    uint8_t ctrl_ifnum = 0;
    bool claimed = false;
};

static int read_sysfs_uint(char const *path, unsigned *out) {
    FILE *f = fopen(path, "r");
    if (!f)
        return -1;
    unsigned v = 0;
    if (fscanf(f, "%u", &v) != 1) {
        fclose(f);
        return -1;
    }
    fclose(f);
    *out = v;
    return 0;
}

/** Open /dev/bus/usb; claim VC interface only (never DISCONNECT — that kills streams). */
static bool usb_uvc_open_for_video(char const *video_dev, UsbUvc *usb) {
    char name[32] = {};
    char sysfs_dev[PATH_MAX];
    char stream_iface[PATH_MAX];
    char ctrl_iface[PATH_MAX];

    if (sscanf(video_dev, "/dev/%31s", name) != 1)
        return false;

    snprintf(sysfs_dev, sizeof(sysfs_dev), "/sys/class/video4linux/%s/device", name);
    if (!realpath(sysfs_dev, stream_iface))
        return false;

    snprintf(ctrl_iface, sizeof(ctrl_iface), "%s", stream_iface);
    char *colon = strrchr(ctrl_iface, ':');
    if (!colon || strlen(colon) < 4)
        return false;
    colon[3] = '0';

    unsigned bus = 0;
    unsigned devnum = 0;
    unsigned ifnum = 0;
    char walk[PATH_MAX];
    snprintf(walk, sizeof(walk), "%s", ctrl_iface);

    for (int depth = 0; depth < 12; ++depth) {
        char p[PATH_MAX];
        snprintf(p, sizeof(p), "%s/busnum", walk);
        if (access(p, R_OK) != 0) {
            char *slash = strrchr(walk, '/');
            if (!slash || slash == walk)
                return false;
            *slash = '\0';
            continue;
        }
        if (read_sysfs_uint(p, &bus) != 0)
            return false;
        snprintf(p, sizeof(p), "%s/devnum", walk);
        if (read_sysfs_uint(p, &devnum) != 0)
            return false;
        snprintf(p, sizeof(p), "%s/bInterfaceNumber", ctrl_iface);
        if (read_sysfs_uint(p, &ifnum) != 0)
            return false;
        break;
    }

    if (!bus || !devnum)
        return false;

    char usb_path[64];
    snprintf(usb_path, sizeof(usb_path), "/dev/bus/usb/%03u/%03u", bus, devnum);
    usb->fd = open(usb_path, O_RDWR | O_CLOEXEC);
    if (usb->fd < 0)
        return false;

    usb->ctrl_ifnum = static_cast<uint8_t>(ifnum);
    unsigned ifn = ifnum;
    if (ioctl(usb->fd, USBDEVFS_CLAIMINTERFACE, &ifn) != 0) {
        /* uvcvideo holds VC while streaming; still try SET_CUR without claim */
        usb->claimed = false;
    } else {
        usb->claimed = true;
    }
    return true;
}

static void usb_uvc_close(UsbUvc *usb) {
    if (usb->fd >= 0 && usb->claimed) {
        unsigned ifn = usb->ctrl_ifnum;
        ioctl(usb->fd, USBDEVFS_RELEASEINTERFACE, &ifn);
        usb->claimed = false;
    }
    if (usb->fd >= 0)
        close(usb->fd);
    usb->fd = -1;
}

static int usb_uvc_set_cur(
    UsbUvc const *usb, uint8_t unit, uint8_t selector, void const *data, uint16_t size) {
    if (usb->fd < 0 || !data || !size)
        return -1;

    struct usbdevfs_ctrltransfer ctrl {};
    ctrl.bRequestType = USB_DIR_OUT | USB_TYPE_CLASS | USB_RECIP_INTERFACE;
    ctrl.bRequest = UVC_SET_CUR;
    ctrl.wValue = static_cast<uint16_t>(selector << 8);
    ctrl.wIndex = static_cast<uint16_t>((unit << 8) | usb->ctrl_ifnum);
    ctrl.wLength = size;
    ctrl.timeout = 1000;
    ctrl.data = const_cast<unsigned char *>(static_cast<unsigned char const *>(data));
    return ioctl(usb->fd, USBDEVFS_CONTROL, &ctrl);
}

/** UVC SET_CUR via uvcvideo on the open capture node (no usbfs claim). */
static int uvc_set_cur(int fd, uint8_t unit, uint8_t sel, void const *data, uint16_t sz) {
    if (fd < 0 || !data || !sz || sz > k_xu_tools_max_len)
        return -1;

    uint8_t buf[k_xu_tools_max_len]{};
    memcpy(buf, data, sz);

    uvc_xu_control_query q{};
    q.unit = unit;
    q.selector = sel;
    q.query = UVC_SET_CUR;
    q.size = sz;
    q.data = buf;
    return ioctl(fd, UVCIOC_CTRL_QUERY, &q);
}

static int uvc_get_cur(int fd, uint8_t unit, uint8_t sel, void *data, uint16_t sz) {
    if (fd < 0 || !data || !sz || sz > k_xu_tools_max_len)
        return -1;

    uvc_xu_control_query q{};
    q.unit = unit;
    q.selector = sel;
    q.query = UVC_GET_CUR;
    q.size = sz;
    q.data = static_cast<uint8_t *>(data);
    return ioctl(fd, UVCIOC_CTRL_QUERY, &q);
}

static uint16_t uvc_xu_get_len(int fd, uint8_t unit, uint8_t sel) {
    uint8_t buf[2]{};
    uvc_xu_control_query q{};
    q.unit = unit;
    q.selector = sel;
    q.query = UVC_GET_LEN;
    q.size = sizeof(buf);
    q.data = buf;
    if (ioctl(fd, UVCIOC_CTRL_QUERY, &q) != 0)
        return k_xu_tools_max_len;
    return static_cast<uint16_t>(buf[0] | (static_cast<unsigned>(buf[1]) << 8));
}

static int
usb_uvc_get_cur(UsbUvc const *usb, uint8_t unit, uint8_t selector, void *data, uint16_t size) {
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
    return ioctl(usb->fd, USBDEVFS_CONTROL, &ctrl);
}

/** uvcvideo requires SET/GET size == UVC_GET_LEN exactly (else ENOBUFS). */
static bool rmsl_xu_query_len(int fd, uint16_t *len_out) {
    if (fd < 0)
        return false;
    uint16_t len = uvc_xu_get_len(fd, k_uvc_unit_xu, k_xu_sel_tools);
    if (len == 0 || len > k_xu_tools_max_len)
        len = k_xu_tools_max_len;
    if (len_out)
        *len_out = len;
    return true;
}

static bool
rmsl_xu_set_padded(int fd, UsbUvc const *usb, void const *payload, uint16_t payload_sz) {
    uint16_t xu_len = 0;
    if (!rmsl_xu_query_len(fd, &xu_len))
        return false;
    if (payload_sz > xu_len)
        return false;

    uint8_t buf[k_xu_tools_max_len]{};
    if (payload && payload_sz)
        std::memcpy(buf, payload, payload_sz);

    if (uvc_set_cur(fd, k_uvc_unit_xu, k_xu_sel_tools, buf, xu_len) == 0)
        return true;

    int err = errno;
    if (usb && usb->fd >= 0 &&
        usb_uvc_set_cur(usb, k_uvc_unit_xu, k_xu_sel_tools, buf, xu_len) == 0)
        return true;
    errno = err;
    return false;
}

static bool rmsl_xu_get_padded(int fd, UsbUvc const *usb, void *out, uint16_t out_sz) {
    uint16_t xu_len = 0;
    if (!rmsl_xu_query_len(fd, &xu_len) || out_sz > xu_len)
        return false;

    uint8_t buf[k_xu_tools_max_len]{};
    if (uvc_get_cur(fd, k_uvc_unit_xu, k_xu_sel_tools, buf, xu_len) == 0) {
        std::memcpy(out, buf, out_sz);
        return true;
    }

    int err = errno;
    if (usb && usb->fd >= 0 &&
        usb_uvc_get_cur(usb, k_uvc_unit_xu, k_xu_sel_tools, buf, xu_len) == 0) {
        std::memcpy(out, buf, out_sz);
        return true;
    }
    errno = err;
    return false;
}

static bool rmsl_xu_open_usb(StreamSession *sess, UsbUvc *usb) {
    if (!sess || !usb || sess->devpath.empty())
        return false;
    if (usb->fd >= 0)
        return true;
    return usb_uvc_open_for_video(sess->devpath.c_str(), usb);
}

static bool rmsl_xu_select_for_get(int fd, UsbUvc const *usb, uint32_t cmd) {
    uint8_t payload[8]{};
    std::memcpy(payload, &cmd, sizeof(cmd));
    int32_t query = k_rmsl_xu_query_val;
    std::memcpy(payload + 4, &query, sizeof(query));
    return rmsl_xu_set_padded(fd, usb, payload, sizeof(payload));
}

static bool rmsl_xu_set_int(StreamSession *sess, uint32_t cmd, int32_t val) {
    if (!sess || !sess->capture.is_open())
        return false;

    UsbUvc usb{};
    rmsl_xu_open_usb(sess, &usb);

    uint8_t payload[8]{};
    std::memcpy(payload, &cmd, sizeof(cmd));
    std::memcpy(payload + 4, &val, sizeof(val));
    if (!rmsl_xu_set_padded(sess->capture.fd(), &usb, payload, sizeof(payload)))
        return false;
    usleep(k_uvc_ctrl_interval_us);
    usb_uvc_close(&usb);
    return true;
}

static bool rmsl_xu_get_int(StreamSession *sess, uint32_t cmd, int32_t *val) {
    if (!sess || !sess->capture.is_open() || !val)
        return false;

    UsbUvc usb{};
    rmsl_xu_open_usb(sess, &usb);
    uint8_t raw[k_xu_tools_max_len]{};
    uint16_t n = 0;

    if (!rmsl_xu_select_for_get(sess->capture.fd(), &usb, cmd))
        goto fail;
    usleep(k_uvc_ctrl_interval_us);

    /* Chunked response: [len u16][data]; strip the length prefix. */
    if (!rmsl_xu_get_padded(sess->capture.fd(), &usb, raw, sizeof(raw)))
        goto fail;
    n = static_cast<uint16_t>(raw[0] | (static_cast<unsigned>(raw[1]) << 8));
    if (n < sizeof(*val))
        goto fail;
    std::memcpy(val, raw + 2, sizeof(*val));

    usb_uvc_close(&usb);
    return true;

fail:
    usb_uvc_close(&usb);
    return false;
}

static uint32_t rmsl_btnr_cmd(StreamSession *sess) {
    if (sess == g_rgb_sess)
        return k_rmsl_cmd_rgb_btnr;
    if (sess == g_ir_sess)
        return k_rmsl_cmd_ir_btnr;
    return 0;
}

static bool parse_on_off(char const *s, int32_t *out) {
    if (!s || !out)
        return false;
    if (strcmp(s, "0") == 0 || strcasecmp(s, "off") == 0 || strcasecmp(s, "disable") == 0) {
        *out = 0;
        return true;
    }
    if (strcmp(s, "1") == 0 || strcasecmp(s, "on") == 0 || strcasecmp(s, "enable") == 0) {
        *out = 1;
        return true;
    }
    char *end = nullptr;
    errno = 0;
    long v = strtol(s, &end, 0);
    if (errno == 0 && end && *end == '\0' && v >= 0 && v <= 1) {
        *out = static_cast<int32_t>(v);
        return true;
    }
    return false;
}

static int32_t clamp_v4l2(int32_t val, int32_t minv, int32_t maxv) {
    if (val < minv)
        return minv;
    if (val > maxv)
        return maxv;
    return val;
}

static bool
v4l2_set(unsigned lane, int fd, uint32_t id, int32_t val, char const *name, bool quiet = false) {
    v4l2_queryctrl qc{};
    qc.id = id;
    if (ioctl(fd, VIDIOC_QUERYCTRL, &qc) != 0) {
        if (!quiet)
            cam_log(lane, "V4L2 %-24s NA\n", name);
        return false;
    }
    if (qc.flags & V4L2_CTRL_FLAG_DISABLED) {
        if (!quiet)
            cam_log(lane, "V4L2 %-24s disabled\n", name);
        return false;
    }
    val = clamp_v4l2(val, qc.minimum, qc.maximum);
    v4l2_control c{id, val};
    if (ioctl(fd, VIDIOC_S_CTRL, &c) != 0) {
        if (!quiet)
            cam_log(lane, "V4L2 %-24s = %d FAIL (%s)\n", name, val, strerror(errno));
        return false;
    }
    if (!quiet)
        cam_log(lane, "V4L2 %-24s = %d ok\n", name, val);
    return true;
}

static bool v4l2_get(int fd, uint32_t id, int32_t *val) {
    v4l2_control c{id, 0};
    if (ioctl(fd, VIDIOC_G_CTRL, &c) != 0)
        return false;
    *val = c.value;
    return true;
}

struct UvcCtrlFallback {
    uint8_t unit;
    uint8_t selector;
    uint16_t size;
};

static bool uvc_fallback_for_v4l2(uint32_t v4l2_id, UvcCtrlFallback *out) {
    if (!out)
        return false;
    struct Map {
        uint32_t v4l2_id;
        uint8_t unit;
        uint8_t selector;
        uint16_t size;
    };
    static Map const k_map[] = {
        {V4L2_CID_BRIGHTNESS, k_uvc_unit_pu, UVC_PU_BRIGHTNESS_CONTROL, 2},
        {V4L2_CID_CONTRAST, k_uvc_unit_pu, UVC_PU_CONTRAST_CONTROL, 2},
        {V4L2_CID_HUE, k_uvc_unit_pu, UVC_PU_HUE_CONTROL, 2},
        {V4L2_CID_SATURATION, k_uvc_unit_pu, UVC_PU_SATURATION_CONTROL, 2},
        {V4L2_CID_SHARPNESS, k_uvc_unit_pu, UVC_PU_SHARPNESS_CONTROL, 2},
        {V4L2_CID_GAMMA, k_uvc_unit_pu, UVC_PU_GAMMA_CONTROL, 2},
        {V4L2_CID_WHITE_BALANCE_TEMPERATURE,
         k_uvc_unit_pu,
         UVC_PU_WHITE_BALANCE_TEMPERATURE_CONTROL,
         2},
        {V4L2_CID_GAIN, k_uvc_unit_pu, UVC_PU_GAIN_CONTROL, 2},
        {V4L2_CID_HUE_AUTO, k_uvc_unit_pu, UVC_PU_HUE_AUTO_CONTROL, 1},
        {V4L2_CID_EXPOSURE_AUTO, k_uvc_unit_ct, UVC_CT_AE_MODE_CONTROL, 1},
    };
    for (Map const &m : k_map) {
        if (m.v4l2_id == v4l2_id) {
            out->unit = m.unit;
            out->selector = m.selector;
            out->size = m.size;
            return true;
        }
    }
    return false;
}

static bool uvc_set_int(int fd, UvcCtrlFallback const *fb, int32_t val) {
    if (!fb || fd < 0)
        return false;
    if (fb->size == 1) {
        uint8_t u8 = static_cast<uint8_t>(val & 0xff);
        return uvc_set_cur(fd, fb->unit, fb->selector, &u8, 1) == 0;
    }
    if (fb->size == 2) {
        uint16_t u16 = static_cast<uint16_t>(val & 0xffff);
        return uvc_set_cur(fd, fb->unit, fb->selector, &u16, 2) == 0;
    }
    return false;
}

static void disable_hue_auto_for_manual_hue(unsigned lane, int fd) {
    v4l2_set(lane, fd, V4L2_CID_HUE_AUTO, 0, "hue_auto", true);
    uint8_t off = 0;
    uvc_set_cur(fd, k_uvc_unit_pu, UVC_PU_HUE_AUTO_CONTROL, &off, 1);
    usleep(k_uvc_ctrl_interval_us);
}

static bool v4l2_query_range(
    int fd, uint32_t id, int32_t *minv, int32_t *maxv, char *name_out, size_t name_len) {
    v4l2_queryctrl qc{};
    qc.id = id;
    if (ioctl(fd, VIDIOC_QUERYCTRL, &qc) != 0)
        return false;
    if (qc.flags & V4L2_CTRL_FLAG_DISABLED)
        return false;
    *minv = qc.minimum;
    *maxv = qc.maximum;
    if (name_out && name_len > 0)
        snprintf(name_out, name_len, "%s", qc.name);
    return true;
}

enum class CtrlValueType {
    INT,
    FLOAT_SEC,
    FLOAT_GAIN,
    EXPOSURE_MODE,
    HUE_AUTO,
    POWER_LINE,
    BTNR,
};

struct CtrlSpec {
    char const *name;
    uint32_t v4l2_id;
    CtrlValueType type;
    char const *note;
};

static const CtrlSpec k_ctrl_specs[] = {
    {"brightness", V4L2_CID_BRIGHTNESS, CtrlValueType::INT, "integer"},
    {"contrast", V4L2_CID_CONTRAST, CtrlValueType::INT, "integer"},
    {"hue", V4L2_CID_HUE, CtrlValueType::INT, "integer"},
    {"saturation", V4L2_CID_SATURATION, CtrlValueType::INT, "integer"},
    {"sharpness", V4L2_CID_SHARPNESS, CtrlValueType::INT, "integer"},
    {"gamma", V4L2_CID_GAMMA, CtrlValueType::INT, "integer"},
    {"wb_temp", V4L2_CID_WHITE_BALANCE_TEMPERATURE, CtrlValueType::INT, "Kelvin"},
    {"exposure_mode",
     V4L2_CID_EXPOSURE_AUTO,
     CtrlValueType::EXPOSURE_MODE,
     "auto|manual|shutter|aperture or 0..3"},
    {"gain", V4L2_CID_GAIN, CtrlValueType::FLOAT_GAIN, "gain multiplier, e.g. 1.5 (=150 wire)"},
    {"exposure_time",
     V4L2_CID_EXPOSURE_ABSOLUTE,
     CtrlValueType::FLOAT_SEC,
     "seconds, e.g. 0.01 (=10ms); set exposure_mode manual first"},
    {"hue_auto", V4L2_CID_HUE_AUTO, CtrlValueType::HUE_AUTO, "0=off 1=on"},
    {"power_line_frequency",
     V4L2_CID_POWER_LINE_FREQUENCY,
     CtrlValueType::POWER_LINE,
     "0=disabled 1=50Hz 2=60Hz"},
    {"btnr", 0, CtrlValueType::BTNR, "Bayer TNR enable 0|1 (rgb/ir via ctl)"},
};

static CtrlSpec const *find_ctrl_spec(char const *name) {
    if (!name)
        return nullptr;
    for (CtrlSpec const &s : k_ctrl_specs) {
        if (strcmp(s.name, name) == 0)
            return &s;
    }
    return nullptr;
}

/** V4L2 S_CTRL with UVC SET_CUR fallback (uvcvideo may EPERM hue while hue_auto is on). */
static bool set_stream_ctrl(unsigned lane, int fd, CtrlSpec const *spec, int32_t val) {
    if (spec->v4l2_id == V4L2_CID_HUE)
        disable_hue_auto_for_manual_hue(lane, fd);

    if (v4l2_set(lane, fd, spec->v4l2_id, val, spec->name, true))
        return true;

    UvcCtrlFallback fb{};
    if (!uvc_fallback_for_v4l2(spec->v4l2_id, &fb))
        return false;

    int32_t minv = val;
    int32_t maxv = val;
    if (v4l2_query_range(fd, spec->v4l2_id, &minv, &maxv, nullptr, 0))
        val = clamp_v4l2(val, minv, maxv);

    return uvc_set_int(fd, &fb, val);
}

static StreamSession *resolve_stream(char const *name) {
    if (!name)
        return nullptr;
    if (strcmp(name, "rgb") == 0)
        return g_rgb_sess;
    if (strcmp(name, "depth") == 0)
        return g_depth_sess;
    if (strcmp(name, "ir") == 0)
        return g_ir_sess;
    return nullptr;
}

static void fps_counter_init(FpsCounter *c) {
    if (!c)
        return;
    std::lock_guard<std::mutex> lk(c->snap_mtx);
    c->frames.store(0);
    c->frames_at_last = 0;
    c->t0 = c->t_last = std::chrono::steady_clock::now();
    c->inited = true;
}

static void fps_counter_tick(FpsCounter *c) {
    if (c)
        c->frames.fetch_add(1, std::memory_order_relaxed);
}

static void
fps_counter_snapshot(FpsCounter *c, double *interval_fps, double *avg_fps, uint64_t *total_frames) {
    using clock = std::chrono::steady_clock;
    std::lock_guard<std::mutex> lk(c->snap_mtx);
    auto now = clock::now();
    uint64_t n = c->frames.load(std::memory_order_relaxed);
    *total_frames = n;

    if (!c->inited) {
        *interval_fps = 0.0;
        *avg_fps = 0.0;
        return;
    }

    double dt_interval = std::chrono::duration<double>(now - c->t_last).count();
    double dt_total = std::chrono::duration<double>(now - c->t0).count();

    if (dt_interval >= 0.05)
        *interval_fps = static_cast<double>(n - c->frames_at_last) / dt_interval;
    else
        *interval_fps = 0.0;

    if (dt_total >= 0.05)
        *avg_fps = static_cast<double>(n) / dt_total;
    else
        *avg_fps = 0.0;

    c->t_last = now;
    c->frames_at_last = n;
}

static char const *stream_label(StreamSession *sess) {
    if (sess == g_rgb_sess)
        return "rgb";
    if (sess == g_depth_sess)
        return "depth";
    if (sess == g_ir_sess)
        return "ir";
    return "?";
}

static void print_stream_fps(StreamSession *sess) {
    if (!sess || !sess->capture.is_open()) {
        printf("%s: not running\n", stream_label(sess));
        return;
    }

    double cap_interval = 0.0;
    double cap_avg = 0.0;
    uint64_t cap_total = 0;
    fps_counter_snapshot(&sess->capture_fps, &cap_interval, &cap_avg, &cap_total);
    printf("%s capture: %.2f fps (since last get), %.2f fps (avg), frames=%" PRIu64 "\n",
           stream_label(sess),
           cap_interval,
           cap_avg,
           cap_total);

    if (g_drm && is_active_preview(sess)) {
        double disp_interval = 0.0;
        double disp_avg = 0.0;
        uint64_t disp_total = 0;
        fps_counter_snapshot(&sess->display_fps, &disp_interval, &disp_avg, &disp_total);
        printf("%s display:  %.2f fps (since last get), %.2f fps (avg), frames=%" PRIu64 "\n",
               stream_label(sess),
               disp_interval,
               disp_avg,
               disp_total);
    }
}

static void print_all_fps() {
    print_stream_fps(g_rgb_sess);
    print_stream_fps(g_depth_sess);
    print_stream_fps(g_ir_sess);
}

static bool parse_3a_stream_prefix(int argc, char **argv, int *idx, StreamSession **sess_out) {
    if (!idx || !sess_out || *idx >= argc)
        return false;

    *sess_out = active_camera_sess();
    if (strcmp(argv[*idx], "rgb") == 0) {
        *sess_out = g_rgb_sess;
        (*idx)++;
    } else if (strcmp(argv[*idx], "ir") == 0) {
        *sess_out = g_ir_sess;
        (*idx)++;
    }
    return *idx < argc;
}

static char const *exposure_mode_str(int32_t v) {
    switch (v) {
    case 0: return "auto";
    case 1: return "manual";
    case 2: return "shutter";
    case 3: return "aperture";
    default: return "?";
    }
}

static bool parse_exposure_mode(char const *s, int32_t *out) {
    if (!s || !out)
        return false;
    char *end = nullptr;
    long v = strtol(s, &end, 0);
    if (end && *end == '\0') {
        *out = static_cast<int32_t>(v);
        return true;
    }
    if (strcmp(s, "auto") == 0) {
        *out = 0;
        return true;
    }
    if (strcmp(s, "manual") == 0) {
        *out = 1;
        return true;
    }
    if (strcmp(s, "shutter") == 0) {
        *out = 2;
        return true;
    }
    if (strcmp(s, "aperture") == 0) {
        *out = 3;
        return true;
    }
    return false;
}

static void trim_line(char *s) {
    if (!s)
        return;
    size_t n = strlen(s);
    while (n > 0 && (s[n - 1] == '\n' || s[n - 1] == '\r' || s[n - 1] == ' ' || s[n - 1] == '\t'))
        s[--n] = '\0';
    char *p = s;
    while (*p == ' ' || *p == '\t')
        ++p;
    if (p != s)
        memmove(s, p, strlen(p) + 1);
}

static int split_words(char *line, char **argv, int max_argv) {
    int n = 0;
    char *p = line;
    while (n < max_argv) {
        while (*p == ' ' || *p == '\t')
            ++p;
        if (*p == '\0')
            break;
        argv[n++] = p;
        while (*p && *p != ' ' && *p != '\t')
            ++p;
        if (*p == '\0')
            break;
        *p++ = '\0';
    }
    return n;
}

static void cmd_help() {
    printf("Commands:\n");
    printf("  help\n");
    printf("  camera [rgb|ir]          (preview + default 3A target; current: %s)\n",
           active_camera_name());
    printf("  set [rgb|ir] <param> <value>   (omit rgb|ir -> active camera)\n");
    printf("  get [rgb|ir] <param>           (omit rgb|ir -> active camera)\n");
    printf("  get [rgb|depth|ir] fps   (host capture/display frame rate)\n");
    printf("  get fps                  (all streams)\n");
    printf("  quit\n\n");
    printf("Parameters (3A on rgb or ir stream):\n");

    StreamSession *ref = active_camera_sess();
    if (!ref || !ref->capture.is_open())
        ref = g_rgb_sess;

    for (CtrlSpec const &s : k_ctrl_specs) {
        int32_t minv = 0;
        int32_t maxv = 0;
        char v4l2_name[64]{};
        bool have = false;

        if (ref && ref->capture.is_open()) {
            std::lock_guard<std::mutex> lk(ref->fd_mtx);
            have = v4l2_query_range(ref->capture.fd(), s.v4l2_id, &minv, &maxv, v4l2_name, sizeof(v4l2_name));
        }

        if (s.type == CtrlValueType::FLOAT_SEC) {
            double min_sec = minv * 0.0001;
            double max_sec = maxv * 0.0001;
            printf("  %-22s [%g .. %g] sec  (%s)\n", s.name, min_sec, max_sec, s.note);
        } else if (s.type == CtrlValueType::FLOAT_GAIN) {
            double min_gain = minv * 0.01;
            double max_gain = maxv * 0.01;
            printf("  %-22s [%g .. %g]x  (%s)\n", s.name, min_gain, max_gain, s.note);
        } else if (s.type == CtrlValueType::EXPOSURE_MODE) {
            printf("  %-22s auto|manual|shutter|aperture (0..3)  (%s)\n", s.name, s.note);
        } else if (s.type == CtrlValueType::BTNR) {
            printf("  %-22s 0=off 1=on  (%s)\n", s.name, s.note);
        } else if (have) {
            printf("  %-22s [%d .. %d]  (%s)\n", s.name, minv, maxv, s.note);
        } else {
            printf("  %-22s  (%s)\n", s.name, s.note);
        }
    }
    printf("\nExamples:\n");
    printf("  camera ir\n");
    printf("  set exposure_mode manual\n");
    printf("  set exposure_time 0.01\n");
    printf("  set rgb gain 1.5\n");
    printf("  set ir btnr 1\n");
    printf("  get rgb btnr\n");
    printf("  get exposure_mode\n");
    printf("  get fps\n");
    if (!g_ir_controls_available)
        printf("\nNote: no IR UVC node was found; ir 3A commands are disabled.\n");
    printf("  get rgb fps\n");
}

static void cmd_camera(int argc, char **argv) {
    if (argc < 2) {
        printf("camera: %s\n", active_camera_name());
        return;
    }

    ActiveCamera next = ActiveCamera::RGB;
    StreamSession *sess = g_rgb_sess;
    if (strcmp(argv[1], "rgb") == 0) {
        next = ActiveCamera::RGB;
        sess = g_rgb_sess;
    } else if (strcmp(argv[1], "ir") == 0) {
        next = ActiveCamera::IR;
        sess = g_ir_sess;
    } else {
        printf("usage: camera rgb|ir\n");
        return;
    }

    if (!sess || !sess->capture.is_open()) {
        printf("camera %s not ready\n", argv[1]);
        return;
    }

    g_active_camera.store(next);
    if (g_drm && next == ActiveCamera::RGB)
        g_drm->setup_layout(static_cast<unsigned>(sess->capture.width()), static_cast<unsigned>(sess->capture.height()));
    printf("camera: %s (preview + default 3A)\n", argv[1]);
}

static void cmd_set(int argc, char **argv) {
    if (argc < 3) {
        printf("usage: set [rgb|ir] <param> <value>\n");
        return;
    }

    int idx = 1;
    StreamSession *sess = nullptr;
    if (!parse_3a_stream_prefix(argc, argv, &idx, &sess)) {
        printf("usage: set [rgb|ir] <param> <value>\n");
        return;
    }

    if (argc < idx + 2) {
        printf("usage: set [rgb|ir] <param> <value>\n");
        return;
    }

    char const *param = argv[idx];
    char const *val_str = argv[idx + 1];
    CtrlSpec const *spec = find_ctrl_spec(param);
    if (!spec) {
        printf("unknown param: %s\n", param);
        return;
    }
    if (!sess || !sess->capture.is_open()) {
        printf("stream not ready\n");
        return;
    }

    int32_t ival = 0;
    if (spec->type == CtrlValueType::BTNR) {
        const uint32_t cmd = rmsl_btnr_cmd(sess);
        if (cmd == 0) {
            printf("btnr only supported on rgb or ir stream\n");
            return;
        }
        if (!parse_on_off(val_str, &ival)) {
            printf("invalid btnr: %s (use 0|1|on|off)\n", val_str);
            return;
        }
        std::lock_guard<std::mutex> lk(sess->fd_mtx);
        if (!rmsl_xu_set_int(sess, cmd, ival)) {
            printf("set btnr failed (%s)\n", strerror(errno));
            return;
        }
        printf("ok: %s btnr = %s\n", sess == g_ir_sess ? "ir" : "rgb", ival ? "on" : "off");
        return;
    }

    if (spec->type == CtrlValueType::EXPOSURE_MODE) {
        if (!parse_exposure_mode(val_str, &ival)) {
            printf("invalid exposure_mode: %s\n", val_str);
            return;
        }
    } else if (spec->type == CtrlValueType::FLOAT_SEC) {
        char *end = nullptr;
        errno = 0;
        double sec = strtod(val_str, &end);
        if (!end || *end != '\0' || errno != 0) {
            printf("invalid exposure_time: %s\n", val_str);
            return;
        }
        ival = static_cast<int32_t>(sec * 10000.0 + (sec >= 0 ? 0.5 : -0.5));
    } else if (spec->type == CtrlValueType::FLOAT_GAIN) {
        char *end = nullptr;
        errno = 0;
        double gain = strtod(val_str, &end);
        if (!end || *end != '\0' || errno != 0) {
            printf("invalid gain: %s\n", val_str);
            return;
        }
        ival = static_cast<int32_t>(gain * 100.0 + (gain >= 0 ? 0.5 : -0.5));
    } else {
        char *end = nullptr;
        errno = 0;
        long v = strtol(val_str, &end, 0);
        if (!end || *end != '\0' || errno != 0) {
            printf("invalid value: %s\n", val_str);
            return;
        }
        ival = static_cast<int32_t>(v);
    }

    std::lock_guard<std::mutex> lk(sess->fd_mtx);
    if (spec->type == CtrlValueType::EXPOSURE_MODE && ival == 1)
        usleep(k_v4l2_ctrl_settle_us);
    if (!set_stream_ctrl(sess->lane, sess->capture.fd(), spec, ival)) {
        printf("set %s failed (%s)\n", param, strerror(errno));
        return;
    }
    if (spec->type == CtrlValueType::EXPOSURE_MODE || spec->type == CtrlValueType::FLOAT_SEC)
        usleep(k_v4l2_ctrl_settle_us);

    if (spec->type == CtrlValueType::FLOAT_SEC)
        printf("ok: %s %s = %.6f sec\n", sess == g_ir_sess ? "ir" : "rgb", param, ival * 0.0001);
    else if (spec->type == CtrlValueType::FLOAT_GAIN)
        printf("ok: %s %s = %.2f\n", sess == g_ir_sess ? "ir" : "rgb", param, ival * 0.01);
    else if (spec->type == CtrlValueType::EXPOSURE_MODE)
        printf("ok: %s %s = %s (%d)\n",
               sess == g_ir_sess ? "ir" : "rgb",
               param,
               exposure_mode_str(ival),
               ival);
    else
        printf("ok: %s %s = %d\n", sess == g_ir_sess ? "ir" : "rgb", param, ival);
}

static void cmd_get(int argc, char **argv) {
    if (argc < 2) {
        printf("usage: get [rgb|depth|ir] <param>|fps\n");
        return;
    }

    if (strcmp(argv[1], "fps") == 0) {
        print_all_fps();
        return;
    }

    if (argc >= 3 && strcmp(argv[2], "fps") == 0) {
        StreamSession *sess = resolve_stream(argv[1]);
        if (!sess) {
            printf("unknown stream: %s (use rgb|depth|ir)\n", argv[1]);
            return;
        }
        print_stream_fps(sess);
        return;
    }

    int idx = 1;
    StreamSession *sess = nullptr;
    if (!parse_3a_stream_prefix(argc, argv, &idx, &sess)) {
        printf("usage: get [rgb|ir] <param>|fps\n");
        return;
    }

    if (argc < idx + 1) {
        printf("usage: get [rgb|ir] <param>|fps\n");
        return;
    }

    char const *param = argv[idx];
    CtrlSpec const *spec = find_ctrl_spec(param);
    if (!spec) {
        printf("unknown param: %s\n", param);
        return;
    }
    if (!sess || !sess->capture.is_open()) {
        printf("stream not ready\n");
        return;
    }

    int32_t val = 0;
    std::lock_guard<std::mutex> lk(sess->fd_mtx);
    if (spec->type == CtrlValueType::BTNR) {
        const uint32_t cmd = rmsl_btnr_cmd(sess);
        if (cmd == 0) {
            printf("btnr only supported on rgb or ir stream\n");
            return;
        }
        if (!rmsl_xu_get_int(sess, cmd, &val)) {
            printf("get btnr failed (%s)\n", strerror(errno));
            return;
        }
        printf("%s btnr = %s (%d)\n", sess == g_ir_sess ? "ir" : "rgb", val ? "on" : "off", val);
        return;
    }

    if (!v4l2_get(sess->capture.fd(), spec->v4l2_id, &val)) {
        printf("get %s failed (%s)\n", param, strerror(errno));
        return;
    }

    if (spec->type == CtrlValueType::FLOAT_SEC)
        printf("%s %s = %.6f sec\n", sess == g_ir_sess ? "ir" : "rgb", param, val * 0.0001);
    else if (spec->type == CtrlValueType::FLOAT_GAIN)
        printf("%s %s = %.2f\n", sess == g_ir_sess ? "ir" : "rgb", param, val * 0.01);
    else if (spec->type == CtrlValueType::EXPOSURE_MODE)
        printf("%s %s = %s (%d)\n",
               sess == g_ir_sess ? "ir" : "rgb",
               param,
               exposure_mode_str(val),
               val);
    else
        printf("%s %s = %d\n", sess == g_ir_sess ? "ir" : "rgb", param, val);
}

static void cli_loop() {
    printf("uvc_host_app ready. camera=%s. Type 'help' or 'quit'.\n", active_camera_name());
    char line[512];
    while (!g_app_stop.load() && fputs("> ", stdout),
           fflush(stdout),
           fgets(line, sizeof(line), stdin)) {
        trim_line(line);
        if (line[0] == '\0')
            continue;
        if (strcmp(line, "quit") == 0 || strcmp(line, "exit") == 0)
            break;

        char *argv[8]{};
        int argc = split_words(line, argv, 8);
        if (argc <= 0)
            continue;

        if (strcmp(argv[0], "help") == 0) {
            cmd_help();
        } else if (strcmp(argv[0], "camera") == 0) {
            cmd_camera(argc, argv);
        } else if (strcmp(argv[0], "set") == 0) {
            cmd_set(argc, argv);
        } else if (strcmp(argv[0], "get") == 0) {
            cmd_get(argc, argv);
        } else {
            printf("unknown command: %s (try help)\n", argv[0]);
        }
    }
    g_app_stop.store(true);
}

static void preview_capture_display_loop(StreamSession *sess) {
    unsigned lane = sess->lane;
    uint32_t const width = static_cast<uint32_t>(sess->capture.width());
    uint32_t const height = static_cast<uint32_t>(sess->capture.height());
    uint32_t const pixfmt = sess->capture.pixfmt();

    while (!sess->stop.load() && !g_app_stop.load()) {
        if (!sess->capture.poll_readable(std::chrono::milliseconds(200)))
            continue;

        v4l2_buffer b{};
        void const *ptr = nullptr;
        {
            std::lock_guard<std::mutex> lk(sess->fd_mtx);
            try {
                if (!sess->capture.try_dequeue(b)) {
                    if (errno == EAGAIN)
                        continue;
                    if (errno != ENODEV && !sess->stop.load())
                        sess->worker_ret.store(1);
                    return;
                }
                ptr = sess->capture.buffer(b.index).data();
            } catch (...) {
                if (!sess->stop.load())
                    sess->worker_ret.store(1);
                return;
            }
        }

        fps_counter_tick(&sess->capture_fps);

        if (g_drm && ptr && is_active_preview(sess)) {
            if (sess->lane == k_lane_ir)
                present_ir_frame(ptr, pixfmt, width, height);
            else
                g_drm->present_lane(lane, ptr, pixfmt, width, height);
            fps_counter_tick(&sess->display_fps);
        }

        {
            std::lock_guard<std::mutex> lk(sess->fd_mtx);
            try {
                sess->capture.queue(b);
            } catch (...) {
                if (errno != ENODEV && !sess->stop.load())
                    sess->worker_ret.store(1);
                return;
            }
        }
    }
}

static void consumer_loop(StreamSession *sess) {
    while (!sess->stop.load() && !g_app_stop.load()) {
        if (!sess->capture.poll_readable(std::chrono::milliseconds(200)))
            continue;

        v4l2_buffer b{};
        {
            std::lock_guard<std::mutex> lk(sess->fd_mtx);
            try {
                if (!sess->capture.try_dequeue(b)) {
                    if (errno == EAGAIN)
                        continue;
                    if (errno != ENODEV && !sess->stop.load())
                        sess->worker_ret.store(1);
                    return;
                }
            } catch (...) {
                if (!sess->stop.load())
                    sess->worker_ret.store(1);
                return;
            }

            fps_counter_tick(&sess->capture_fps);

            try {
                sess->capture.queue(b);
            } catch (...) {
                if (errno != ENODEV)
                    sess->worker_ret.store(1);
                return;
            }
        }
    }
}

static int stream_session_open(StreamSession *sess, CaptureProfile const &prof) {
    sess->lane = (prof.card_match == k_profile_rmsl_rgb.card_match)     ? k_lane_rgb
                 : (prof.card_match == k_profile_rmsl_depth.card_match) ? k_lane_depth
                                                                        : k_lane_ir;

    if (!sess->capture.open(sess->devpath)) {
        cam_perror(sess->lane, sess->devpath.c_str());
        return 1;
    }

    unsigned nbuf = (sess->lane == k_lane_rgb || sess->lane == k_lane_ir) ? k_v4l2_nbuffers_rgb
                                                                          : k_v4l2_nbuffers;
    std::vector<uint32_t> fallbacks(k_pix_fallback.begin(), k_pix_fallback.end());
    bool configured = false;

    if (sess->lane == k_lane_ir) {
        if (sess->capture.configure(
                {V4L2_PIX_FMT_NV12, 1080, static_cast<int>(k_ir_combine_h), prof.format.fps},
                nbuf)) {
            cam_log(sess->lane, "IR combine 1080x%u\n", k_ir_combine_h);
            configured = true;
        }
    }
    if (!configured &&
        !sess->capture.configure_with_fallback(prof.format, fallbacks, nbuf)) {
        sess->capture.close();
        return 1;
    }
    if (!sess->capture.start()) {
        cam_perror(sess->lane, "VIDIOC_STREAMON");
        sess->capture.close();
        return 1;
    }

    fps_counter_init(&sess->capture_fps);
    fps_counter_init(&sess->display_fps);
    return 0;
}

static void stream_session_close(StreamSession *sess) {
    if (!sess)
        return;
    if (sess->worker.joinable())
        sess->worker.join();
    sess->capture.close();
}

static void stream_worker(StreamSession *sess) {
    if (g_drm && (sess->lane == k_lane_rgb || sess->lane == k_lane_ir))
        preview_capture_display_loop(sess);
    else
        consumer_loop(sess);
}

static int
stream_session_start(StreamSession *sess, std::string devpath, CaptureProfile const &prof) {
    sess->devpath = std::move(devpath);
    sess->stop.store(false);
    sess->worker_ret.store(0);
    if (stream_session_open(sess, prof) != 0) {
        stream_session_close(sess);
        return 1;
    }
    sess->worker = std::thread(stream_worker, sess);
    return 0;
}

static void stream_session_stop(StreamSession *sess) {
    if (!sess)
        return;
    sess->stop.store(true);
    stream_session_close(sess);
}

int run_rmsl321_app() {
    auto rgb_dev =
        uvc_tools::find_capture_device_any({"RMSL321_RGB", "RMSL322_RGB"});
    auto depth_dev =
        uvc_tools::find_capture_device_any({"RMSL321_DEPTH", "RMSL322_DEPTH"});
    auto ir_dev =
        uvc_tools::find_capture_device_any({"RMSL321_IR", "RMSL322_IR"});
    bool ir_available = ir_dev.has_value();

    if (!rgb_dev || !depth_dev) {
        fprintf(stderr,
                "need RGB+DEPTH (found rgb=%d depth=%d ir=%d)\n",
                rgb_dev.has_value(),
                depth_dev.has_value(),
                ir_dev.has_value());
        return 1;
    }
    if (!ir_available)
        fprintf(stderr, "INFO: no IR UVC node found; ir 3A controls disabled\n");

    uvc_tools::DrmScreen drm;
    if (g_drm_enable) {
        if (drm.init()) {
            g_drm = &drm;
            drm.setup_layout(k_profile_rmsl_rgb.format.width, k_profile_rmsl_rgb.format.height);
        } else {
            fprintf(stderr, "WARN: DRM init failed, continue without display\n");
            g_drm = nullptr;
        }
    }

    StreamSession sess_depth{};
    StreamSession sess_rgb{};
    StreamSession sess_ir{};
    g_rgb_sess = &sess_rgb;
    g_depth_sess = &sess_depth;
    g_ir_sess = ir_available ? &sess_ir : nullptr;
    g_ir_controls_available = ir_available;

    g_app_stop.store(false);

    if (stream_session_start(&sess_depth, *depth_dev, k_profile_rmsl_depth) != 0 ||
        stream_session_start(&sess_rgb, *rgb_dev, k_profile_rmsl_rgb) != 0) {
        g_app_stop.store(true);
        stream_session_stop(&sess_depth);
        stream_session_stop(&sess_rgb);
        g_drm = nullptr;
        return 1;
    }

    std::thread ir_start_thread;
    if (ir_available) {
        ir_start_thread = std::thread([&] {
            std::this_thread::sleep_for(std::chrono::milliseconds(k_ir_start_delay_ms));
            if (!g_app_stop.load() && stream_session_start(&sess_ir, *ir_dev, k_profile_rmsl_ir) != 0)
                fprintf(stderr, "WARN: IR stream start failed\n");
        });
    }

    cli_loop();

    if (ir_start_thread.joinable())
        ir_start_thread.join();
    stream_session_stop(&sess_depth);
    stream_session_stop(&sess_rgb);
    if (ir_available)
        stream_session_stop(&sess_ir);

    g_rgb_sess = nullptr;
    g_depth_sess = nullptr;
    g_ir_sess = nullptr;
    g_ir_controls_available = false;
    g_drm = nullptr;

    const int ir_ret = ir_available ? sess_ir.worker_ret.load() : 0;
    return (sess_depth.worker_ret.load() || sess_rgb.worker_ret.load() || ir_ret) ? 1 : 0;
}

} // namespace

int main(int argc, char **argv) {
    for (int i = 1; i < argc; ++i) {
        if (strcmp(argv[i], "--no-drm") == 0)
            g_drm_enable = false;
    }
    return run_rmsl321_app();
}
