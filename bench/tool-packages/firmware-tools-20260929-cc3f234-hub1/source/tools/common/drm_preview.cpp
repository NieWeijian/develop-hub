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

#include "drm_preview.hpp"

#include <algorithm>
#include <array>
#include <atomic>
#include <cerrno>
#include <chrono>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <fcntl.h>
#include <iostream>
#include <mutex>
#include <optional>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <vector>
#include <system_error>
#include <unistd.h>
#include <vector>

#include <linux/videodev2.h>
#include <drm/drm.h>
#include <drm/drm_mode.h>
#include <libdrm/drm_fourcc.h>
#include <xf86drm.h>
#include <xf86drmMode.h>

namespace uvc_tools {
namespace {

constexpr int kDrmPreviewBufferCount = 3;
constexpr int kPreviewPanelWidth = 960;
constexpr int kPreviewPanelHeight = 1080;
constexpr uint32_t kDrmModeTypePreferred = 1u << 3;


/* Current firmware embeds UvcHeadV2 at the start of raw video buffers.  Host
 * capture strips it before display, then this renderer restores a zeroed prefix
 * so pixel addressing still matches the module-side stride. */
constexpr size_t kUvcHeaderSize = 40;

/* Every IR frame is two back-to-back NV12 halves
 * ([Y0][UV0][Y1][UV1]). Rearrange that combined layout into
 * [Y0][Y1][UV0][UV1] before rendering, independent of output scale. */
bool rearrange_combined_ir(uint8_t *buf, int w, int h) {
    if (buf == nullptr || w <= 0 || h <= 0 || (h & 1) != 0)
        return false;
    const size_t hh = static_cast<size_t>(h) / 2;
    const size_t y_sz = static_cast<size_t>(w) * hh;
    const size_t uv_sz = y_sz / 2;
    std::vector<uint8_t> uv0(uv_sz);
    std::memcpy(uv0.data(), buf + y_sz, uv_sz); /* save UV0 */
    std::memmove(buf + y_sz, buf + y_sz + uv_sz, y_sz); /* Y1 -> Y plane tail */
    std::memcpy(buf + y_sz * 2, uv0.data(), uv_sz);     /* UV0 -> UV plane head */
    return true;
}

constexpr uint32_t kDrmModeConnected = 1;
constexpr uint32_t kDrmFourccXrgb8888 = 0x34325258;
constexpr uint32_t kDrmFourccArgb8888 = 0x34325241;
constexpr uint32_t kDrmPlaneTypeOverlay = 0;
constexpr uint32_t kDrmPlaneTypePrimary = 1;

template <typename T>
uint64_t to_drm_user_ptr(T *ptr) {
    return static_cast<uint64_t>(reinterpret_cast<uintptr_t>(ptr));
}

size_t frame_pixel_bytes(int width, int height, bool is_nv12) {
    return is_nv12 ? static_cast<size_t>(width) * height * 3 / 2
                   : static_cast<size_t>(width) * height * 2;
}

bool prepare_embedded_frame(const PreviewFrame &frame,
                            size_t pixel_bytes,
                            std::vector<uint8_t> &scratch) {
    if (scratch.size() != pixel_bytes)
        scratch.resize(pixel_bytes);
    if (frame.payload.size() >= pixel_bytes) {
        std::memcpy(scratch.data(), frame.payload.data(), pixel_bytes);
        return true;
    }
    if (frame.payload.size() + kUvcHeaderSize < pixel_bytes)
        return false;
    std::memset(scratch.data(), 0, kUvcHeaderSize);
    const size_t copy_size = std::min(frame.payload.size(), pixel_bytes - kUvcHeaderSize);
    std::memcpy(scratch.data() + kUvcHeaderSize, frame.payload.data(), copy_size);
    return true;
}

void drm_preview_ioctl(int fd, unsigned long request, void *arg, const char *where) {
    while (::ioctl(fd, request, arg) < 0) {
        if (errno != EINTR) {
            std::string msg = where;
            msg += ": drm ioctl failed";
            throw std::system_error(errno, std::generic_category(), msg);
        }
    }
}

struct PreviewLayout {
    int screen_w = 0;
    int screen_h = 0;
    int panel_w = kPreviewPanelWidth;
    int panel_h = kPreviewPanelHeight;
    int panel_count = 2;
    bool panels_horizontal = true;
    bool rotate_cw90 = false;
};

void put_bgra(uint8_t *dst, uint8_t b, uint8_t g, uint8_t r) {
    dst[0] = b;
    dst[1] = g;
    dst[2] = r;
    dst[3] = 0xFF;
}

void map_composite_to_screen(const PreviewLayout &layout, int cx, int cy, int &sx, int &sy) {
    if (layout.rotate_cw90) {
        /* Counter-clockwise 90°: the landscape composite (1920x1080) is
         * rotated to fill the portrait screen (1080x1920). */
        sx = cy;
        sy = (layout.panels_horizontal ? layout.panel_w * layout.panel_count : layout.panel_w) -
             1 - cx;
        return;
    }

    const int comp_w =
        layout.panels_horizontal ? layout.panel_w * layout.panel_count : layout.panel_w;
    const int comp_h =
        layout.panels_horizontal ? layout.panel_h : layout.panel_h * layout.panel_count;
    sx = cx + (layout.screen_w - comp_w) / 2;
    sy = cy + (layout.screen_h - comp_h) / 2;
}

uint16_t read_depth_sample(std::byte const *payload, size_t payload_bytes, size_t pixel_index) {
    constexpr size_t kHeaderPixels = kUvcHeaderSize / sizeof(uint16_t);
    if (pixel_index < kHeaderPixels)
        return 0;
    size_t const idx = pixel_index - kHeaderPixels;
    size_t const off = idx * 2;
    if (off + 1 >= payload_bytes)
        return 0;
    auto const *bytes = reinterpret_cast<uint8_t const *>(payload);
    return static_cast<uint16_t>(bytes[off]) | (static_cast<uint16_t>(bytes[off + 1]) << 8);
}

/* Fixed physical depth range (mm) used by the color mapping, following the
 * RMSL32X reference demo (rkros_rgbd_cam): depth is clamped into a fixed
 * window so the same distance always maps to the same color, instead of a
 * per-frame stretch that shifts with scene content. Near limit -> red, far
 * limit -> blue, 0 (no data) stays black. The default window [100, 1500] mm
 * matches the module's practical working envelope (measured depth p50 sits
 * around 0.7-1.0 m); the demo's wider 40-3000 mm window spreads the same
 * content over only the red/yellow part of the ramp.
 * Runtime override for tuning (e.g. RMSL_DEPTH_MIN=200 RMSL_DEPTH_MAX=1500):
 * useful to match the module's real working envelope per deployment. */
constexpr uint16_t kDepthColorMinMm = 100;
constexpr uint16_t kDepthColorMaxMm = 1500;

struct DepthColorRange {
    uint16_t lo = kDepthColorMinMm;
    uint16_t hi = kDepthColorMaxMm;
};

DepthColorRange depth_color_range() {
    DepthColorRange range;
    if (char const *value = std::getenv("RMSL_DEPTH_MIN")) {
        long const parsed = std::strtol(value, nullptr, 10);
        if (parsed > 0 && parsed < range.hi)
            range.lo = static_cast<uint16_t>(parsed);
    }
    if (char const *value = std::getenv("RMSL_DEPTH_MAX")) {
        long const parsed = std::strtol(value, nullptr, 10);
        if (parsed > range.lo && parsed <= 65535)
            range.hi = static_cast<uint16_t>(parsed);
    }
    return range;
}

struct DepthStats {
    uint16_t min = 0;
    uint16_t p2 = 0;
    uint16_t p50 = 0;
    uint16_t p98 = 0;
    uint16_t max = 0;
    size_t valid = 0;
};

/* One-pass 256-bin histogram over valid (non-zero) depth samples, reporting
 * percentiles so the fixed color range can be calibrated to the module's real
 * working envelope instead of being guessed. */
DepthStats depth_payload_stats(PreviewFrame const &frame) {
    DepthStats stats;
    if (frame.width <= 0 || frame.height <= 0)
        return stats;
    size_t const count = static_cast<size_t>(frame.width) * frame.height;
    uint32_t hist[256] = {};
    uint64_t total = 0;
    uint16_t min_sample = 0xFFFF;
    uint16_t max_sample = 0;
    for (size_t i = 0; i < count; ++i) {
        uint16_t const s =
            read_depth_sample(frame.payload.data(), frame.payload.size(), i);
        if (s == 0)
            continue;
        ++hist[s >> 8];
        ++total;
        min_sample = std::min(min_sample, s);
        max_sample = std::max(max_sample, s);
    }
    stats.min = min_sample == 0xFFFF ? 0 : min_sample;
    stats.max = max_sample;
    stats.valid = total;
    if (total < 64)
        return stats;

    auto percentile_bucket = [&](uint64_t pct) -> unsigned {
        uint64_t const target = total * pct / 100;
        uint64_t acc = 0;
        for (unsigned b = 0; b < 256; ++b) {
            acc += hist[b];
            if (acc >= target)
                return b;
        }
        return 255;
    };

    stats.p2 = static_cast<uint16_t>(percentile_bucket(2) << 8);
    stats.p50 = static_cast<uint16_t>(percentile_bucket(50) << 8);
    stats.p98 = static_cast<uint16_t>((percentile_bucket(98) << 8) | 0xFF);
    return stats;
}

/* Depth colormap: near (small depth) = red, far (large depth) = blue.
 * Continuous red -> yellow -> green -> cyan -> blue ramp so close objects pop
 * as warm colors and background fades to cool blue. */
inline void depth_colormap(float t, uint8_t &r, uint8_t &g, uint8_t &b) {
    if (t < 0.0f)
        t = 0.0f;
    else if (t > 1.0f)
        t = 1.0f;
    if (t < 0.25f) {
        r = 255;
        g = static_cast<uint8_t>(t * 4.0f * 255.0f + 0.5f);
        b = 0;
    } else if (t < 0.5f) {
        r = static_cast<uint8_t>((0.5f - t) * 4.0f * 255.0f + 0.5f);
        g = 255;
        b = 0;
    } else if (t < 0.75f) {
        r = 0;
        g = 255;
        b = static_cast<uint8_t>((t - 0.5f) * 4.0f * 255.0f + 0.5f);
    } else {
        r = 0;
        g = static_cast<uint8_t>((1.0f - t) * 4.0f * 255.0f + 0.5f);
        b = 255;
    }
}

/* Build a 16-bit depth -> BGRA lookup table for a given peak (near=red,
 * far=blue). Indexing by the raw sample avoids a per-pixel divide. */
void build_depth_lut(uint16_t lo, uint16_t hi, std::vector<uint8_t> &lut) {
    if (lut.size() != static_cast<size_t>(65536) * 4)
        lut.resize(static_cast<size_t>(65536) * 4);
    if (hi <= lo)
        hi = static_cast<uint16_t>(lo + 1);
    float const span = static_cast<float>(hi - lo);
    for (uint32_t i = 0; i < 65536; ++i) {
        uint8_t r = 0, g = 0, b = 0;
        if (i != 0) { /* depth 0 = no data -> black */
            float const t = std::clamp((static_cast<float>(i) - lo) / span, 0.0f, 1.0f);
            depth_colormap(t, r, g, b);
        }
        lut[i * 4 + 0] = b;
        lut[i * 4 + 1] = g;
        lut[i * 4 + 2] = r;
        lut[i * 4 + 3] = 255;
    }
}

struct PanelFit {
    int out_w = 0;
    int out_h = 0;
    int off_x = 0;
    int off_y = 0;
};

PanelFit compute_panel_fit(int src_w, int src_h, int panel_w, int panel_h) {
    PanelFit fit;
    if (src_w <= 0 || src_h <= 0 || panel_w <= 0 || panel_h <= 0)
        return fit;

    const double scale =
        std::min(static_cast<double>(panel_w) / src_w, static_cast<double>(panel_h) / src_h);
    fit.out_w = std::max(1, static_cast<int>(src_w * scale + 0.5));
    fit.out_h = std::max(1, static_cast<int>(src_h * scale + 0.5));
    fit.off_x = (panel_w - fit.out_w) / 2;
    fit.off_y = (panel_h - fit.out_h) / 2;
    return fit;
}

void render_nv12_panel(const uint8_t *nv12,
                       int src_w,
                       int src_h,
                       const PreviewLayout &layout,
                       int panel_index,
                       uint8_t *fb,
                       uint32_t pitch,
                       int fb_w,
                       int fb_h) {
    const int pw = layout.panel_w;
    const int ph = layout.panel_h;
    const int comp_x = layout.panels_horizontal ? panel_index * pw : 0;
    const int comp_y = layout.panels_horizontal ? 0 : panel_index * ph;
    const PanelFit fit = compute_panel_fit(src_w, src_h, pw, ph);
    const uint8_t *y_plane = nv12;
    const uint8_t *uv_plane = nv12 + static_cast<size_t>(src_w) * src_h;

    for (int dy = 0; dy < fit.out_h; ++dy) {
        const int src_y = dy * src_h / fit.out_h;
        const int y_row = src_y * src_w;
        const int uv_row = (src_y / 2) * src_w;
        for (int dx = 0; dx < fit.out_w; ++dx) {
            const int src_x = dx * src_w / fit.out_w;
            const int y = y_plane[y_row + src_x];
            const int uv_idx = uv_row + (src_x & ~1);
            const int u = static_cast<int>(uv_plane[uv_idx]) - 128;
            const int v = static_cast<int>(uv_plane[uv_idx + 1]) - 128;
            const int c = y - 16;
            const int r = (298 * c + 409 * v + 128) >> 8;
            const int g = (298 * c - 100 * u - 208 * v + 128) >> 8;
            const int b = (298 * c + 516 * u + 128) >> 8;

            int sx = 0;
            int sy = 0;
            map_composite_to_screen(layout, comp_x + fit.off_x + dx, comp_y + fit.off_y + dy, sx, sy);
            if (sx < 0 || sx >= fb_w || sy < 0 || sy >= fb_h)
                continue;
            put_bgra(fb + static_cast<size_t>(sy) * pitch + static_cast<size_t>(sx) * 4,
                     static_cast<uint8_t>(std::clamp(b, 0, 255)),
                     static_cast<uint8_t>(std::clamp(g, 0, 255)),
                     static_cast<uint8_t>(std::clamp(r, 0, 255)));
        }
    }
}

void render_color_depth_panel(PreviewFrame const &frame,
                              PreviewLayout const &layout,
                              int panel_index,
                              uint8_t *fb,
                              uint32_t pitch,
                              int fb_w,
                              int fb_h,
                              std::vector<uint8_t> &depth_lut) {
    int const src_w = frame.width;
    int const src_h = frame.height;
    if (src_w <= 0 || src_h <= 0)
        return;

    const int pw = layout.panel_w;
    const int ph = layout.panel_h;
    const int comp_x = layout.panels_horizontal ? panel_index * pw : 0;
    const int comp_y = layout.panels_horizontal ? 0 : panel_index * ph;
    const PanelFit fit = compute_panel_fit(src_w, src_h, pw, ph);
    DepthColorRange const range = depth_color_range();
    build_depth_lut(range.lo, range.hi, depth_lut);

    /* Throttled depth stats on stderr: calibrate the fixed color range to the
     * real scene envelope (p2/p50/p98 of valid samples, in mm). */
    static auto last_stats_time = std::chrono::steady_clock::now();
    auto const now = std::chrono::steady_clock::now();
    if (now - last_stats_time >= std::chrono::seconds(5)) {
        DepthStats const stats = depth_payload_stats(frame);
        std::cerr << "depth: color range [" << range.lo << ',' << range.hi
                  << "] mm valid=" << stats.valid << " min=" << stats.min
                  << " p2=" << stats.p2 << " p50=" << stats.p50 << " p98=" << stats.p98
                  << " max=" << stats.max << '\n';
        last_stats_time = now;
    }

    const bool rot = layout.rotate_cw90;
    const int comp_w = layout.panels_horizontal ? pw * layout.panel_count : pw;
    const int comp_h = layout.panels_horizontal ? ph : ph * layout.panel_count;
    const int base_x = (layout.screen_w - comp_w) / 2;
    const int base_y = (layout.screen_h - comp_h) / 2;

    for (int dy = 0; dy < fit.out_h; ++dy) {
        const int src_y = dy * src_h / fit.out_h;
        const int src_row = src_y * src_w;
        const int local_y = comp_y + fit.off_y + dy;
        int sy = 0;
        if (rot)
            sy = comp_w - 1 - (comp_x + fit.off_x); /* updated per dx below */
        else
            sy = local_y + base_y;

        for (int dx = 0; dx < fit.out_w; ++dx) {
            const int src_x = dx * src_w / fit.out_w;
            uint16_t const sample =
                read_depth_sample(frame.payload.data(), frame.payload.size(), src_row + src_x);
            const uint8_t *px = &depth_lut[static_cast<size_t>(sample) * 4];

            int sx = 0;
            if (rot) {
                sx = local_y;
                sy = comp_w - 1 - (comp_x + fit.off_x + dx);
                if (sx < 0 || sx >= fb_w)
                    continue;
                if (sy < 0 || sy >= fb_h)
                    continue;
                uint8_t *row = fb + static_cast<size_t>(sy) * pitch;
                std::memcpy(row + static_cast<size_t>(sx) * 4, px, 4);
            } else {
                sx = comp_x + fit.off_x + dx + base_x;
                if (sx < 0 || sx >= fb_w)
                    continue;
                if (sy < 0 || sy >= fb_h)
                    continue;
                uint8_t *row = fb + static_cast<size_t>(sy) * pitch;
                std::memcpy(row + static_cast<size_t>(sx) * 4, px, 4);
            }
        }
    }
}

PreviewLayout compute_preview_layout(int screen_w, int screen_h, int panel_count = 2) {
    PreviewLayout layout;
    layout.screen_w = screen_w;
    layout.screen_h = screen_h;
    layout.panel_count = panel_count;

    const int comp_w = kPreviewPanelWidth * panel_count;
    const int comp_h = kPreviewPanelHeight;
    const bool screen_portrait = screen_h > screen_w;
    const bool comp_landscape = comp_w > comp_h;

    if (screen_portrait && comp_landscape && comp_w == screen_h && comp_h == screen_w) {
        layout.rotate_cw90 = true;
        return layout;
    }

    if (screen_portrait && comp_landscape && comp_w <= screen_h && comp_h <= screen_w) {
        layout.rotate_cw90 = true;
        return layout;
    }

    if (screen_portrait) {
        layout.panels_horizontal = false;
        layout.panel_w = screen_w;
        layout.panel_h = screen_h / panel_count;
        return layout;
    }

    if (comp_w <= screen_w && comp_h <= screen_h)
        return layout;

    layout.panel_w = screen_w / panel_count;
    layout.panel_h = screen_h;
    return layout;
}
struct DrmResources {
    drm_mode_modeinfo mode{};
    uint32_t crtc_id = 0;
    uint32_t connector_id = 0;
};

struct DumbBuffer {
    uint32_t handle = 0;
    uint32_t fb_id = 0;
    uint32_t pitch = 0;
    uint32_t width = 0;
    uint32_t height = 0;
    uint8_t *map = nullptr;
    size_t map_size = 0;
};

void destroy_dumb_buffer(int fd, DumbBuffer &buf) {
    if (buf.map != nullptr) {
        ::munmap(buf.map, buf.map_size);
        buf.map = nullptr;
        buf.map_size = 0;
    }
    if (buf.fb_id != 0) {
        uint32_t fb_id = buf.fb_id;
        ::ioctl(fd, DRM_IOCTL_MODE_RMFB, &fb_id);
        buf.fb_id = 0;
    }
    if (buf.handle != 0) {
        drm_mode_destroy_dumb destroy{};
        destroy.handle = buf.handle;
        ::ioctl(fd, DRM_IOCTL_MODE_DESTROY_DUMB, &destroy);
        buf.handle = 0;
    }
}

void create_dumb_buffer(int fd, uint32_t width, uint32_t height, DumbBuffer &buf) {
    destroy_dumb_buffer(fd, buf);

    drm_mode_create_dumb create{};
    create.width = width;
    create.height = height;
    create.bpp = 32;
    drm_preview_ioctl(fd, DRM_IOCTL_MODE_CREATE_DUMB, &create, "CREATE_DUMB");

    drm_mode_fb_cmd addfb{};
    addfb.width = create.width;
    addfb.height = create.height;
    addfb.pitch = create.pitch;
    addfb.bpp = 32;
    addfb.depth = 24;
    addfb.handle = create.handle;
    drm_preview_ioctl(fd, DRM_IOCTL_MODE_ADDFB, &addfb, "ADDFB");

    drm_mode_map_dumb map_req{};
    map_req.handle = create.handle;
    drm_preview_ioctl(fd, DRM_IOCTL_MODE_MAP_DUMB, &map_req, "MAP_DUMB");

    void *mapped = ::mmap(nullptr,
                          create.size,
                          PROT_READ | PROT_WRITE,
                          MAP_SHARED,
                          fd,
                          static_cast<off_t>(map_req.offset));
    if (mapped == MAP_FAILED)
        throw std::system_error(errno, std::generic_category(), "drm mmap failed");

    buf.handle = create.handle;
    buf.fb_id = addfb.fb_id;
    buf.pitch = create.pitch;
    buf.width = create.width;
    buf.height = create.height;
    buf.map = static_cast<uint8_t *>(mapped);
    buf.map_size = create.size;
}

void destroy_dumb_buffers(int fd, std::array<DumbBuffer, kDrmPreviewBufferCount> &bufs) {
    for (auto &buf : bufs)
        destroy_dumb_buffer(fd, buf);
}

void create_dumb_buffers(int fd,
                         uint32_t width,
                         uint32_t height,
                         std::array<DumbBuffer, kDrmPreviewBufferCount> &bufs) {
    destroy_dumb_buffers(fd, bufs);
    for (auto &buf : bufs)
        create_dumb_buffer(fd, width, height, buf);
}

bool drm_get_resources(int fd,
                       std::vector<uint32_t> &fb_ids,
                       std::vector<uint32_t> &crtcs,
                       std::vector<uint32_t> &connectors,
                       std::vector<uint32_t> &encoders) {
    drm_mode_card_res res{};
    if (::ioctl(fd, DRM_IOCTL_MODE_GETRESOURCES, &res) < 0)
        return false;

    fb_ids.resize(res.count_fbs);
    crtcs.resize(res.count_crtcs);
    connectors.resize(res.count_connectors);
    encoders.resize(res.count_encoders);

    res.fb_id_ptr = fb_ids.empty() ? 0 : to_drm_user_ptr(fb_ids.data());
    res.crtc_id_ptr = crtcs.empty() ? 0 : to_drm_user_ptr(crtcs.data());
    res.connector_id_ptr = connectors.empty() ? 0 : to_drm_user_ptr(connectors.data());
    res.encoder_id_ptr = encoders.empty() ? 0 : to_drm_user_ptr(encoders.data());
    drm_preview_ioctl(fd, DRM_IOCTL_MODE_GETRESOURCES, &res, "GETRESOURCES");
    return true;
}

bool drm_query_connector(int fd, uint32_t connector_id, DrmResources &out) {
    drm_mode_get_connector conn{};
    conn.connector_id = connector_id;
    if (::ioctl(fd, DRM_IOCTL_MODE_GETCONNECTOR, &conn) < 0)
        return false;

    if (conn.connection != kDrmModeConnected || conn.count_modes == 0 || conn.count_encoders == 0)
        return false;

    const uint32_t mode_count = conn.count_modes;
    const uint32_t encoder_count = conn.count_encoders;
    const uint32_t prop_count = conn.count_props;

    std::vector<uint32_t> encoders(encoder_count);
    std::vector<drm_mode_modeinfo> modes(mode_count);
    std::vector<uint32_t> props(prop_count);
    std::vector<uint64_t> prop_values(prop_count);

    conn.count_modes = mode_count;
    conn.count_encoders = encoder_count;
    conn.count_props = prop_count;
    conn.modes_ptr = mode_count ? to_drm_user_ptr(modes.data()) : 0;
    conn.encoders_ptr = encoder_count ? to_drm_user_ptr(encoders.data()) : 0;
    conn.props_ptr = prop_count ? to_drm_user_ptr(props.data()) : 0;
    conn.prop_values_ptr = prop_count ? to_drm_user_ptr(prop_values.data()) : 0;
    drm_preview_ioctl(fd, DRM_IOCTL_MODE_GETCONNECTOR, &conn, "GETCONNECTOR");

    if (conn.count_modes == 0 || conn.count_encoders == 0)
        return false;

    uint32_t mode_idx = 0;
    for (uint32_t i = 0; i < conn.count_modes; ++i) {
        if (modes[i].type & kDrmModeTypePreferred) {
            mode_idx = i;
            break;
        }
    }

    drm_mode_get_encoder encoder{};
    encoder.encoder_id = encoders[0];
    drm_preview_ioctl(fd, DRM_IOCTL_MODE_GETENCODER, &encoder, "GETENCODER");
    if (encoder.crtc_id == 0)
        return false;

    out.connector_id = connector_id;
    out.mode = modes[mode_idx];
    out.crtc_id = encoder.crtc_id;
    return true;
}

bool get_connected_mode(int fd, DrmResources &out) {
    std::vector<uint32_t> fb_ids;
    std::vector<uint32_t> crtcs;
    std::vector<uint32_t> connectors;
    std::vector<uint32_t> encoders;
    if (!drm_get_resources(fd, fb_ids, crtcs, connectors, encoders))
        return false;

    for (uint32_t connector_id : connectors) {
        if (drm_query_connector(fd, connector_id, out))
            return true;
    }
    return false;
}

int drm_crtc_index(int fd, uint32_t crtc_id) {
    std::vector<uint32_t> fb_ids;
    std::vector<uint32_t> crtcs;
    std::vector<uint32_t> connectors;
    std::vector<uint32_t> encoders;
    if (!drm_get_resources(fd, fb_ids, crtcs, connectors, encoders))
        return -1;

    for (size_t i = 0; i < crtcs.size(); ++i) {
        if (crtcs[i] == crtc_id)
            return static_cast<int>(i);
    }
    return -1;
}

bool drm_plane_supports_format(int fd, uint32_t plane_id, uint32_t fourcc) {
    drm_mode_get_plane plane{};
    plane.plane_id = plane_id;
    if (::ioctl(fd, DRM_IOCTL_MODE_GETPLANE, &plane) < 0)
        return false;

    const uint32_t count = plane.count_format_types;
    if (count == 0)
        return false;

    std::vector<uint32_t> formats(count);
    plane.count_format_types = count;
    plane.format_type_ptr = to_drm_user_ptr(formats.data());
    if (::ioctl(fd, DRM_IOCTL_MODE_GETPLANE, &plane) < 0)
        return false;

    return std::find(formats.begin(), formats.end(), fourcc) != formats.end();
}

bool drm_plane_supports_xrgb(int fd, uint32_t plane_id) {
    return drm_plane_supports_format(fd, plane_id, kDrmFourccXrgb8888) ||
           drm_plane_supports_format(fd, plane_id, kDrmFourccArgb8888);
}

std::optional<uint64_t> drm_object_get_property(int fd,
                                                uint32_t obj_id,
                                                uint32_t obj_type,
                                                const char *name) {
    drm_mode_obj_get_properties req{};
    req.obj_id = obj_id;
    req.obj_type = obj_type;
    if (::ioctl(fd, DRM_IOCTL_MODE_OBJ_GETPROPERTIES, &req) < 0)
        return std::nullopt;

    const uint32_t count = req.count_props;
    if (count == 0)
        return std::nullopt;

    std::vector<uint32_t> prop_ids(count);
    std::vector<uint64_t> prop_values(count);
    req.count_props = count;
    req.props_ptr = to_drm_user_ptr(prop_ids.data());
    req.prop_values_ptr = to_drm_user_ptr(prop_values.data());
    if (::ioctl(fd, DRM_IOCTL_MODE_OBJ_GETPROPERTIES, &req) < 0)
        return std::nullopt;

    for (uint32_t i = 0; i < count; ++i) {
        drm_mode_get_property prop{};
        prop.prop_id = prop_ids[i];
        if (::ioctl(fd, DRM_IOCTL_MODE_GETPROPERTY, &prop) < 0)
            continue;
        if (std::strcmp(prop.name, name) == 0)
            return prop_values[i];
    }
    return std::nullopt;
}

std::optional<uint32_t> drm_object_find_property_id(int fd,
                                                    uint32_t obj_id,
                                                    uint32_t obj_type,
                                                    const char *name) {
    drm_mode_obj_get_properties req{};
    req.obj_id = obj_id;
    req.obj_type = obj_type;
    if (::ioctl(fd, DRM_IOCTL_MODE_OBJ_GETPROPERTIES, &req) < 0)
        return std::nullopt;

    const uint32_t count = req.count_props;
    if (count == 0)
        return std::nullopt;

    std::vector<uint32_t> prop_ids(count);
    std::vector<uint64_t> prop_values(count);
    req.count_props = count;
    req.props_ptr = to_drm_user_ptr(prop_ids.data());
    req.prop_values_ptr = to_drm_user_ptr(prop_values.data());
    if (::ioctl(fd, DRM_IOCTL_MODE_OBJ_GETPROPERTIES, &req) < 0)
        return std::nullopt;

    for (uint32_t i = 0; i < count; ++i) {
        drm_mode_get_property prop{};
        prop.prop_id = prop_ids[i];
        if (::ioctl(fd, DRM_IOCTL_MODE_GETPROPERTY, &prop) < 0)
            continue;
        if (std::strcmp(prop.name, name) == 0)
            return prop_ids[i];
    }
    return std::nullopt;
}

void drm_try_set_plane_zpos_max(int fd, uint32_t plane_id) {
    const auto prop_id =
        drm_object_find_property_id(fd, plane_id, DRM_MODE_OBJECT_PLANE, "zpos");
    if (!prop_id)
        return;

    drm_mode_get_property prop{};
    prop.prop_id = *prop_id;
    if (::ioctl(fd, DRM_IOCTL_MODE_GETPROPERTY, &prop) < 0)
        return;

    uint64_t zpos = 255;
    if (prop.count_values >= 2) {
        std::array<uint64_t, 2> range{};
        prop.count_values = 2;
        prop.values_ptr = to_drm_user_ptr(range.data());
        if (::ioctl(fd, DRM_IOCTL_MODE_GETPROPERTY, &prop) == 0)
            zpos = range[1];
    }

    drm_mode_obj_set_property set{};
    set.obj_id = plane_id;
    set.obj_type = DRM_MODE_OBJECT_PLANE;
    set.prop_id = *prop_id;
    set.value = zpos;
    ::ioctl(fd, DRM_IOCTL_MODE_OBJ_SETPROPERTY, &set);
}

struct DisplayPlane {
    uint32_t plane_id = 0;
    bool is_overlay = false;
};

std::optional<DisplayPlane> drm_find_display_plane(int fd, uint32_t crtc_id) {
    const int crtc_idx = drm_crtc_index(fd, crtc_id);
    if (crtc_idx < 0)
        return std::nullopt;

    drm_mode_get_plane_res pres{};
    if (::ioctl(fd, DRM_IOCTL_MODE_GETPLANERESOURCES, &pres) < 0)
        return std::nullopt;

    const uint32_t count = pres.count_planes;
    if (count == 0)
        return std::nullopt;

    std::vector<uint32_t> plane_ids(count);
    pres.count_planes = count;
    pres.plane_id_ptr = to_drm_user_ptr(plane_ids.data());
    if (::ioctl(fd, DRM_IOCTL_MODE_GETPLANERESOURCES, &pres) < 0)
        return std::nullopt;

    std::optional<DisplayPlane> best;
    int best_score = -1;

    for (uint32_t plane_id : plane_ids) {
        drm_mode_get_plane plane{};
        plane.plane_id = plane_id;
        if (::ioctl(fd, DRM_IOCTL_MODE_GETPLANE, &plane) < 0)
            continue;
        if ((plane.possible_crtcs & (1u << crtc_idx)) == 0)
            continue;
        if (!drm_plane_supports_xrgb(fd, plane_id))
            continue;

        const auto type = drm_object_get_property(fd, plane_id, DRM_MODE_OBJECT_PLANE, "type");
        if (!type)
            continue;

        int score = 0;
        bool is_overlay = false;
        if (*type == kDrmPlaneTypeOverlay) {
            score = 100;
            is_overlay = true;
        } else if (*type == kDrmPlaneTypePrimary) {
            score = 50;
        } else {
            continue;
        }

        if (score > best_score) {
            best = DisplayPlane{plane_id, is_overlay};
            best_score = score;
        }
    }
    return best;
}

} // namespace

struct DrmPreviewDisplay::Impl {
    Impl() = default;

    ~Impl() { cleanup(); }

    Impl(Impl const &) = delete;
    Impl &operator=(Impl const &) = delete;

    bool init(const std::string &device_path =
                  "/dev/dri/by-path/platform-display-subsystem-card") {
        cleanup();

        fd_ = ::open(device_path.c_str(), O_RDWR | O_CLOEXEC);
        if (fd_ < 0) {
            std::cerr << "preview: open " << device_path << " failed: " << std::strerror(errno)
                      << '\n';
            return false;
        }

        try {
            if (!get_connected_mode(fd_, resources_)) {
                std::cerr << "preview: no connected DRM connector on " << device_path << '\n';
                cleanup();
                return false;
            }

            create_dumb_buffers(fd_,
                                resources_.mode.hdisplay,
                                resources_.mode.vdisplay,
                                buffers_);
        } catch (const std::system_error &ex) {
            std::cerr << "preview: DRM init failed: " << ex.what() << '\n';
            cleanup();
            return false;
        }

        back_index_ = 0;
        layout_ = compute_preview_layout(static_cast<int>(resources_.mode.hdisplay),
                                         static_cast<int>(resources_.mode.vdisplay));
        layout_all_ = compute_preview_layout(static_cast<int>(resources_.mode.hdisplay),
                                             static_cast<int>(resources_.mode.vdisplay),
                                             3);

        master_acquired_ = (::ioctl(fd_, DRM_IOCTL_SET_MASTER) == 0);
        if (!master_acquired_) {
            std::cerr << "preview: SET_MASTER failed: " << std::strerror(errno)
                      << " (preview may be covered by desktop)\n";
        }

        if (const auto plane = drm_find_display_plane(fd_, resources_.crtc_id)) {
            plane_id_ = plane->plane_id;
            drm_try_set_plane_zpos_max(fd_, plane_id_);
            std::cout << "preview: plane " << plane_id_ << ' '
                      << (plane->is_overlay ? "overlay" : "primary") << " zpos=max\n";
        }

        std::cout << "preview: DRM " << device_path << ' ' << layout_.screen_w << 'x'
                  << layout_.screen_h << (layout_.screen_h > layout_.screen_w ? " portrait" : " landscape")
                  << ", panel " << layout_.panel_w << 'x' << layout_.panel_h << ", buffers "
                  << kDrmPreviewBufferCount;
        if (layout_.rotate_cw90)
            std::cout << ", composite rotate 90 CW";
        else if (!layout_.panels_horizontal)
            std::cout << ", stacked";
        std::cout << '\n';
        return true;
    }

    bool present_composite(const PreviewFrame &rgb, const PreviewFrame &depth) {
        if (fd_ < 0 || rgb.width <= 0 || rgb.height <= 0 || depth.width <= 0 || depth.height <= 0)
            return false;

        const size_t rgb_bytes = frame_pixel_bytes(rgb.width, rgb.height, true);
        if (!prepare_embedded_frame(rgb, rgb_bytes, rgb_scratch_))
            return false;
        if (depth.payload.size() + kUvcHeaderSize <
            frame_pixel_bytes(depth.width, depth.height, false))
            return false;

        DumbBuffer &back = buffers_[back_index_];
        const int fb_w = static_cast<int>(back.width);
        const int fb_h = static_cast<int>(back.height);
        const uint32_t pitch = back.pitch;
        std::memset(back.map, 0, back.map_size);

        render_nv12_panel(rgb_scratch_.data(),
                          rgb.width,
                          rgb.height,
                          layout_,
                          0,
                          back.map,
                          pitch,
                          fb_w,
                          fb_h);
        render_color_depth_panel(depth, layout_, 1, back.map, pitch, fb_w, fb_h, depth_lut_);

        std::atomic_thread_fence(std::memory_order_release);
        if (!present_buffer(back))
            return false;

        back_index_ = (back_index_ + 1) % kDrmPreviewBufferCount;
        return true;
    }

    /* RGB+IR-only view: the second panel shows the combined IR
     * NV12 frame as grayscale instead of the depth colormap. */
    bool present_composite_ir(const PreviewFrame &rgb, const PreviewFrame &ir) {
        if (fd_ < 0 || rgb.width <= 0 || rgb.height <= 0 || ir.width <= 0 || ir.height <= 0)
            return false;

        const size_t rgb_bytes = frame_pixel_bytes(rgb.width, rgb.height, true);
        if (!prepare_embedded_frame(rgb, rgb_bytes, rgb_scratch_))
            return false;
        const size_t ir_bytes = frame_pixel_bytes(ir.width, ir.height, true);
        if (!prepare_embedded_frame(ir, ir_bytes, ir_scratch_))
            return false;
        if (!rearrange_combined_ir(ir_scratch_.data(), ir.width, ir.height))
            return false;

        DumbBuffer &back = buffers_[back_index_];
        const int fb_w = static_cast<int>(back.width);
        const int fb_h = static_cast<int>(back.height);
        const uint32_t pitch = back.pitch;
        std::memset(back.map, 0, back.map_size);

        render_nv12_panel(rgb_scratch_.data(),
                          rgb.width,
                          rgb.height,
                          layout_,
                          0,
                          back.map,
                          pitch,
                          fb_w,
                          fb_h);
        render_nv12_panel(ir_scratch_.data(),
                          ir.width,
                          ir.height,
                          layout_,
                          1,
                          back.map,
                          pitch,
                          fb_w,
                          fb_h);

        std::atomic_thread_fence(std::memory_order_release);
        if (!present_buffer(back))
            return false;

        back_index_ = (back_index_ + 1) % kDrmPreviewBufferCount;
        return true;
    }

    bool present_composite_all(const PreviewFrame &rgb,
                               const PreviewFrame &depth,
                               const PreviewFrame &ir) {
        if (fd_ < 0 || rgb.width <= 0 || rgb.height <= 0 || depth.width <= 0 ||
            depth.height <= 0 || ir.width <= 0 || ir.height <= 0)
            return false;

        const size_t rgb_bytes = frame_pixel_bytes(rgb.width, rgb.height, true);
        if (!prepare_embedded_frame(rgb, rgb_bytes, rgb_scratch_))
            return false;
        if (depth.payload.size() + kUvcHeaderSize <
            frame_pixel_bytes(depth.width, depth.height, false))
            return false;
        const size_t ir_bytes = frame_pixel_bytes(ir.width, ir.height, true);
        if (!prepare_embedded_frame(ir, ir_bytes, ir_scratch_))
            return false;
        if (!rearrange_combined_ir(ir_scratch_.data(), ir.width, ir.height))
            return false;

        DumbBuffer &back = buffers_[back_index_];
        const int fb_w = static_cast<int>(back.width);
        const int fb_h = static_cast<int>(back.height);
        const uint32_t pitch = back.pitch;
        std::memset(back.map, 0, back.map_size);

        render_nv12_panel(rgb_scratch_.data(),
                          rgb.width,
                          rgb.height,
                          layout_all_,
                          0,
                          back.map,
                          pitch,
                          fb_w,
                          fb_h);
        render_color_depth_panel(
            depth, layout_all_, 1, back.map, pitch, fb_w, fb_h, depth_lut_);
        render_nv12_panel(ir_scratch_.data(),
                          ir.width,
                          ir.height,
                          layout_all_,
                          2,
                          back.map,
                          pitch,
                          fb_w,
                          fb_h);

        std::atomic_thread_fence(std::memory_order_release);
        if (!present_buffer(back))
            return false;

        back_index_ = (back_index_ + 1) % kDrmPreviewBufferCount;
        return true;
    }

private:
    bool present_buffer(const DumbBuffer &buf) {
        if (plane_id_ != 0) {
            drm_mode_set_plane plane{};
            plane.plane_id = plane_id_;
            plane.crtc_id = resources_.crtc_id;
            plane.fb_id = buf.fb_id;
            plane.crtc_x = 0;
            plane.crtc_y = 0;
            plane.crtc_w = buf.width;
            plane.crtc_h = buf.height;
            plane.src_x = 0;
            plane.src_y = 0;
            plane.src_w = buf.width << 16;
            plane.src_h = buf.height << 16;
            try {
                drm_preview_ioctl(fd_, DRM_IOCTL_MODE_SETPLANE, &plane, "SETPLANE");
            } catch (const std::system_error &ex) {
                std::cerr << "preview: MODE_SETPLANE failed: " << ex.what() << '\n';
                return false;
            }
            return true;
        }

        drm_mode_crtc crtc{};
        crtc.crtc_id = resources_.crtc_id;
        crtc.fb_id = buf.fb_id;
        crtc.set_connectors_ptr = to_drm_user_ptr(&resources_.connector_id);
        crtc.count_connectors = 1;
        crtc.mode_valid = 1;
        crtc.mode = resources_.mode;
        crtc.x = 0;
        crtc.y = 0;
        try {
            drm_preview_ioctl(fd_, DRM_IOCTL_MODE_SETCRTC, &crtc, "SETCRTC");
            crtc_enabled_ = true;
        } catch (const std::system_error &ex) {
            std::cerr << "preview: MODE_SETCRTC failed: " << ex.what() << '\n';
            return false;
        }
        return true;
    }

    void cleanup() {
        destroy_dumb_buffers(fd_, buffers_);
        if (fd_ >= 0) {
            if (master_acquired_) {
                ::ioctl(fd_, DRM_IOCTL_DROP_MASTER);
                master_acquired_ = false;
            }
            ::close(fd_);
            fd_ = -1;
        }
        crtc_enabled_ = false;
        plane_id_ = 0;
        back_index_ = 0;
    }

    int fd_ = -1;
    DrmResources resources_{};
    PreviewLayout layout_{};
    PreviewLayout layout_all_{};
    std::array<DumbBuffer, kDrmPreviewBufferCount> buffers_{};
    size_t back_index_ = 0;
    std::vector<uint8_t> rgb_scratch_;
    std::vector<uint8_t> ir_scratch_;
    std::vector<uint8_t> depth_lut_;
    uint32_t plane_id_ = 0;
    bool master_acquired_ = false;
    bool crtc_enabled_ = false;
};

DrmPreviewDisplay::DrmPreviewDisplay() : impl_(std::make_unique<Impl>()) {}

DrmPreviewDisplay::~DrmPreviewDisplay() = default;

DrmPreviewDisplay::DrmPreviewDisplay(DrmPreviewDisplay &&other) noexcept = default;

DrmPreviewDisplay &DrmPreviewDisplay::operator=(DrmPreviewDisplay &&other) noexcept = default;

bool DrmPreviewDisplay::init(std::string const &device_path) {
    return impl_->init(device_path);
}

bool DrmPreviewDisplay::present_composite(PreviewFrame const &rgb, PreviewFrame const &depth) {
    return impl_->present_composite(rgb, depth);
}

bool DrmPreviewDisplay::present_composite_ir(PreviewFrame const &rgb, PreviewFrame const &ir) {
    return impl_->present_composite_ir(rgb, ir);
}

bool DrmPreviewDisplay::present_composite_all(PreviewFrame const &rgb,
                                              PreviewFrame const &depth,
                                              PreviewFrame const &ir) {
    return impl_->present_composite_all(rgb, depth, ir);
}

// ---------------------------------------------------------------------------
// DrmScreen (libdrm SetCrtc path)
// ---------------------------------------------------------------------------

namespace screen_detail {
namespace {

constexpr unsigned kLaneCount = 3;
constexpr unsigned kBufCount = 2;

struct ViewRect {
    unsigned x = 0;
    unsigned y = 0;
    unsigned w = 0;
    unsigned h = 0;
};

struct ScreenBuffer {
    uint32_t fb_id = 0;
    uint32_t handle = 0;
    void *map = nullptr;
};

uint32_t yuv_to_argb(int y, int u, int v) {
    int const c = y - 16;
    int const r = (298 * c + 409 * v + 128) >> 8;
    int const g = (298 * c - 100 * u - 208 * v + 128) >> 8;
    int const b = (298 * c + 516 * u + 128) >> 8;
    uint8_t const rr = static_cast<uint8_t>(std::clamp(r, 0, 255));
    uint8_t const gg = static_cast<uint8_t>(std::clamp(g, 0, 255));
    uint8_t const bb = static_cast<uint8_t>(std::clamp(b, 0, 255));
    return 0xFF000000u | (static_cast<uint32_t>(rr) << 16) | (static_cast<uint32_t>(gg) << 8) |
           static_cast<uint32_t>(bb);
}

void fit_aspect_rect(unsigned sw,
                     unsigned sh,
                     unsigned bx,
                     unsigned by,
                     unsigned bw,
                     unsigned bh,
                     unsigned *dx,
                     unsigned *dy,
                     unsigned *dw,
                     unsigned *dh) {
    if (!sw || !sh || !bw || !bh) {
        *dx = bx;
        *dy = by;
        *dw = 0;
        *dh = 0;
        return;
    }

    double const scale = std::min(static_cast<double>(bw) / sw, static_cast<double>(bh) / sh);
    *dw = std::max(1u, static_cast<unsigned>(sw * scale + 0.5));
    *dh = std::max(1u, static_cast<unsigned>(sh * scale + 0.5));
    *dx = bx + (bw - *dw) / 2;
    *dy = by + (bh - *dh) / 2;
}

} // namespace
} // namespace screen_detail

struct DrmScreen::Impl {
    using ViewRect = screen_detail::ViewRect;
    using ScreenBuffer = screen_detail::ScreenBuffer;

    Impl() = default;
    ~Impl() { shutdown(); }

    Impl(Impl const &) = delete;
    Impl &operator=(Impl const &) = delete;

    bool init(char const *device_path);
    void setup_layout(unsigned rgb_w, unsigned rgb_h);
    void present_lane(unsigned lane,
                      void const *src,
                      uint32_t pixfmt,
                      unsigned src_w,
                      unsigned src_h);
    void present_ir_lr(void const *left,
                       void const *right,
                       uint32_t pixfmt,
                       unsigned eye_w,
                       unsigned eye_h);
    void shutdown();

private:
    uint32_t *back_map() const {
        return static_cast<uint32_t *>(bufs_[back_idx_].map);
    }

    void put_px(unsigned x, unsigned y, uint32_t c) {
        if (x >= scr_w_ || y >= scr_h_ || !back_map())
            return;
        back_map()[static_cast<size_t>(y) * pitch_u32_ + x] = c;
    }

    void blit_nv12_1x1_at(uint8_t const *src,
                          unsigned dpitch_u32,
                          unsigned dx,
                          unsigned dy,
                          unsigned w,
                          unsigned h,
                          unsigned src_w,
                          unsigned src_h) {
        if (!src || !w || !h || !src_w || !src_h)
            return;

        uint8_t const *y_plane = src;
        uint8_t const *uv_plane = src + static_cast<size_t>(src_w) * src_h;

        for (unsigned row = 0; row < h; ++row) {
            unsigned const src_y = row * src_h / h;
            unsigned const y_row = src_y * src_w;
            unsigned const uv_row = (src_y / 2) * src_w;
            for (unsigned col = 0; col < w; ++col) {
                unsigned const src_x = col * src_w / w;
                int const y = y_plane[y_row + src_x];
                unsigned const uv_idx = uv_row + (src_x & ~1u);
                int const u = static_cast<int>(uv_plane[uv_idx]) - 128;
                int const v = static_cast<int>(uv_plane[uv_idx + 1]) - 128;
                unsigned const x = dx + col;
                unsigned const y_out = dy + row;
                if (x >= scr_w_ || y_out >= scr_h_)
                    continue;
                back_map()[static_cast<size_t>(y_out) * dpitch_u32 + x] =
                    screen_detail::yuv_to_argb(y, u, v);
            }
        }
    }

    void blit_yuyv_1x1_at(uint8_t const *src,
                          unsigned dpitch_u32,
                          unsigned dx,
                          unsigned dy,
                          unsigned w,
                          unsigned h,
                          unsigned src_w,
                          unsigned src_h) {
        if (!src || !w || !h || !src_w || !src_h)
            return;

        for (unsigned row = 0; row < h; ++row) {
            unsigned const src_y = row * src_h / h;
            for (unsigned col = 0; col < w; ++col) {
                unsigned const src_x = col * src_w / w;
                size_t const pair = (static_cast<size_t>(src_y) * src_w + (src_x & ~1u)) * 2;
                int const y = src[pair];
                int const u = static_cast<int>(src[pair + 1]) - 128;
                int const v = static_cast<int>(src[pair + 3]) - 128;
                unsigned const x = dx + col;
                unsigned const y_out = dy + row;
                if (x >= scr_w_ || y_out >= scr_h_)
                    continue;
                back_map()[static_cast<size_t>(y_out) * dpitch_u32 + x] =
                    screen_detail::yuv_to_argb(y, u, v);
            }
        }
    }

    void blit_scaled(unsigned lane,
                     void const *src,
                     uint32_t pixfmt,
                     unsigned sw,
                     unsigned sh,
                     unsigned bx,
                     unsigned by,
                     unsigned bw,
                     unsigned bh) {
        if (!src || lane >= screen_detail::kLaneCount || !bw || !bh)
            return;

        unsigned dx = 0;
        unsigned dy = 0;
        unsigned dw = 0;
        unsigned dh = 0;
        screen_detail::fit_aspect_rect(sw, sh, bx, by, bw, bh, &dx, &dy, &dw, &dh);
        if (!dw || !dh)
            return;

        src_w_[lane] = sw;
        src_h_[lane] = sh;
        view_[lane] = {dx, dy, dw, dh};

        if (pixfmt == V4L2_PIX_FMT_NV12) {
            blit_nv12_1x1_at(static_cast<uint8_t const *>(src), pitch_u32_, dx, dy, dw, dh, sw, sh);
        } else if (pixfmt == V4L2_PIX_FMT_YUYV) {
            blit_yuyv_1x1_at(static_cast<uint8_t const *>(src), pitch_u32_, dx, dy, dw, dh, sw, sh);
        }
    }

    bool pick_connector_crtc() {
        for (int i = 0; i < resources_->count_connectors; ++i) {
            drmModeConnector *conn =
                drmModeGetConnector(fd_, resources_->connectors[i]);
            if (!conn)
                continue;

            if (conn->connection == DRM_MODE_CONNECTED && conn->count_modes > 0) {
                conn_id_ = conn->connector_id;
                mode_ = conn->modes[0];
                for (int m = 0; m < conn->count_modes; ++m) {
                    if (conn->modes[m].type & DRM_MODE_TYPE_PREFERRED) {
                        mode_ = conn->modes[m];
                        break;
                    }
                }

                if (conn->encoder_id) {
                    drmModeEncoder *enc = drmModeGetEncoder(fd_, conn->encoder_id);
                    if (enc && enc->crtc_id) {
                        crtc_id_ = enc->crtc_id;
                        drmModeFreeEncoder(enc);
                        conn_ = conn;
                        return true;
                    }
                    drmModeFreeEncoder(enc);
                }

                for (int e = 0; e < conn->count_encoders; ++e) {
                    drmModeEncoder *enc = drmModeGetEncoder(fd_, conn->encoders[e]);
                    if (!enc)
                        continue;
                    if (enc->crtc_id) {
                        crtc_id_ = enc->crtc_id;
                        drmModeFreeEncoder(enc);
                        conn_ = conn;
                        return true;
                    }
                    drmModeFreeEncoder(enc);
                }
            }
            drmModeFreeConnector(conn);
        }
        return false;
    }

    bool create_single_dumb(ScreenBuffer *buf) {
        drm_mode_create_dumb create{};
        create.width = scr_w_;
        create.height = scr_h_;
        create.bpp = 32;
        if (::ioctl(fd_, DRM_IOCTL_MODE_CREATE_DUMB, &create) != 0)
            return false;

        uint32_t handles[4] = {create.handle, 0, 0, 0};
        uint32_t pitches[4] = {create.pitch, 0, 0, 0};
        uint32_t offsets[4] = {0, 0, 0, 0};
        if (drmModeAddFB2(fd_,
                          scr_w_,
                          scr_h_,
                          DRM_FORMAT_XRGB8888,
                          handles,
                          pitches,
                          offsets,
                          &buf->fb_id,
                          0) != 0) {
            drm_mode_destroy_dumb destroy{};
            destroy.handle = create.handle;
            ::ioctl(fd_, DRM_IOCTL_MODE_DESTROY_DUMB, &destroy);
            return false;
        }

        drm_mode_map_dumb map_req{};
        map_req.handle = create.handle;
        if (::ioctl(fd_, DRM_IOCTL_MODE_MAP_DUMB, &map_req) != 0) {
            drmModeRmFB(fd_, buf->fb_id);
            buf->fb_id = 0;
            drm_mode_destroy_dumb destroy{};
            destroy.handle = create.handle;
            ::ioctl(fd_, DRM_IOCTL_MODE_DESTROY_DUMB, &destroy);
            return false;
        }

        void *mapped = ::mmap(nullptr,
                              create.size,
                              PROT_READ | PROT_WRITE,
                              MAP_SHARED,
                              fd_,
                              static_cast<off_t>(map_req.offset));
        if (mapped == MAP_FAILED) {
            drmModeRmFB(fd_, buf->fb_id);
            buf->fb_id = 0;
            drm_mode_destroy_dumb destroy{};
            destroy.handle = create.handle;
            ::ioctl(fd_, DRM_IOCTL_MODE_DESTROY_DUMB, &destroy);
            return false;
        }

        buf->handle = create.handle;
        buf->map = mapped;
        pitch_ = create.pitch;
        pitch_u32_ = create.pitch / 4;
        map_size_ = create.size;
        return true;
    }

    bool create_dumb_fbs() {
        for (unsigned i = 0; i < screen_detail::kBufCount; ++i) {
            if (!create_single_dumb(&bufs_[i]))
                return false;
        }
        return true;
    }

    void flip_front(unsigned show_idx) {
        if (fd_ < 0 || show_idx >= screen_detail::kBufCount)
            return;

        int ret = drmModeSetCrtc(fd_,
                                 crtc_id_,
                                 bufs_[show_idx].fb_id,
                                 0,
                                 0,
                                 &conn_id_,
                                 1,
                                 &mode_);
        if (ret != 0 && !flip_err_logged_) {
            std::cerr << "screen: drmModeSetCrtc failed: " << std::strerror(errno) << '\n';
            flip_err_logged_ = true;
        }
    }

    int fd_ = -1;
    drmModeRes *resources_ = nullptr;
    drmModeConnector *conn_ = nullptr;
    drmModeModeInfo mode_{};

    uint32_t conn_id_ = 0;
    uint32_t crtc_id_ = 0;
    uint32_t scr_w_ = 0;
    uint32_t scr_h_ = 0;
    uint32_t content_w_ = 0;
    uint32_t content_h_ = 0;
    uint32_t blit_x_ = 0;
    uint32_t blit_y_ = 0;
    uint32_t pitch_ = 0;
    uint32_t pitch_u32_ = 0;
    size_t map_size_ = 0;
    bool flip_err_logged_ = false;

    ScreenBuffer bufs_[screen_detail::kBufCount]{};
    unsigned back_idx_ = 0;
    unsigned front_idx_ = 0;

    ViewRect view_[screen_detail::kLaneCount]{};
    unsigned src_w_[screen_detail::kLaneCount]{};
    unsigned src_h_[screen_detail::kLaneCount]{};

    std::mutex mtx_;
};

bool DrmScreen::Impl::init(char const *device_path) {
    std::lock_guard<std::mutex> lk(mtx_);
    shutdown();

    fd_ = ::open(device_path, O_RDWR | O_CLOEXEC);
    if (fd_ < 0) {
        std::cerr << "screen: open " << device_path << " failed: " << std::strerror(errno) << '\n';
        return false;
    }

    resources_ = drmModeGetResources(fd_);
    if (!resources_) {
        std::cerr << "screen: drmModeGetResources failed\n";
        shutdown();
        return false;
    }

    if (!pick_connector_crtc()) {
        std::cerr << "screen: no connected DRM output\n";
        shutdown();
        return false;
    }

    scr_w_ = mode_.hdisplay;
    scr_h_ = mode_.vdisplay;
    content_w_ = scr_w_;
    content_h_ = scr_h_;
    blit_x_ = 0;
    blit_y_ = 0;

    if (!create_dumb_fbs()) {
        std::cerr << "screen: dumb buffer create failed\n";
        shutdown();
        return false;
    }

    back_idx_ = 0;
    front_idx_ = 0;
    std::memset(back_map(), 0, map_size_);
    flip_front(0);

    std::cout << "screen: DRM " << device_path << ' ' << scr_w_ << 'x' << scr_h_ << '\n';
    return true;
}

void DrmScreen::Impl::setup_layout(unsigned rgb_w, unsigned rgb_h) {
    std::lock_guard<std::mutex> lk(mtx_);
    if (fd_ < 0 || !rgb_w || !rgb_h)
        return;

    content_w_ = scr_w_;
    content_h_ = scr_h_;
    blit_x_ = 0;
    blit_y_ = 0;

    unsigned dx = 0;
    unsigned dy = 0;
    unsigned dw = 0;
    unsigned dh = 0;
    screen_detail::fit_aspect_rect(rgb_w, rgb_h, blit_x_, blit_y_, content_w_, content_h_, &dx, &dy, &dw, &dh);
    view_[0] = {dx, dy, dw, dh};
    src_w_[0] = rgb_w;
    src_h_[0] = rgb_h;

    unsigned depth_w = 640;
    unsigned depth_h = 352;
    screen_detail::fit_aspect_rect(
        depth_w, depth_h, blit_x_, blit_y_, content_w_, content_h_, &dx, &dy, &dw, &dh);
    view_[1] = {dx, dy, dw, dh};
    src_w_[1] = depth_w;
    src_h_[1] = depth_h;

    view_[2] = {blit_x_, blit_y_, content_w_, content_h_};
}

void DrmScreen::Impl::present_lane(unsigned lane,
                                   void const *src,
                                   uint32_t pixfmt,
                                   unsigned src_w,
                                   unsigned src_h) {
    std::lock_guard<std::mutex> lk(mtx_);
    if (fd_ < 0 || !src || lane >= screen_detail::kLaneCount || !src_w || !src_h)
        return;

    std::memset(back_map(), 0, map_size_);
    blit_scaled(lane,
                src,
                pixfmt,
                src_w,
                src_h,
                blit_x_,
                blit_y_,
                content_w_,
                content_h_);

    flip_front(back_idx_);
    front_idx_ = back_idx_;
    back_idx_ = (back_idx_ + 1) % screen_detail::kBufCount;
}

void DrmScreen::Impl::present_ir_lr(void const *left,
                                    void const *right,
                                    uint32_t pixfmt,
                                    unsigned eye_w,
                                    unsigned eye_h) {
    std::lock_guard<std::mutex> lk(mtx_);
    if (fd_ < 0 || !left || !right || !eye_w || !eye_h || pixfmt != V4L2_PIX_FMT_NV12)
        return;

    std::memset(back_map(), 0, map_size_);

    unsigned half_w = content_w_ / 2;
    unsigned dx = 0;
    unsigned dy = 0;
    unsigned dw = 0;
    unsigned dh = 0;
    screen_detail::fit_aspect_rect(
        eye_w, eye_h, blit_x_, blit_y_, half_w, content_h_, &dx, &dy, &dw, &dh);
    blit_nv12_1x1_at(static_cast<uint8_t const *>(left), pitch_u32_, dx, dy, dw, dh, eye_w, eye_h);

    screen_detail::fit_aspect_rect(eye_w,
                                   eye_h,
                                   blit_x_ + half_w,
                                   blit_y_,
                                   half_w,
                                   content_h_,
                                   &dx,
                                   &dy,
                                   &dw,
                                   &dh);
    blit_nv12_1x1_at(static_cast<uint8_t const *>(right), pitch_u32_, dx, dy, dw, dh, eye_w, eye_h);

    flip_front(back_idx_);
    front_idx_ = back_idx_;
    back_idx_ = (back_idx_ + 1) % screen_detail::kBufCount;
}

void DrmScreen::Impl::shutdown() {
    for (unsigned i = 0; i < screen_detail::kBufCount; ++i) {
        if (bufs_[i].map) {
            ::munmap(bufs_[i].map, map_size_);
            bufs_[i].map = nullptr;
        }
        if (fd_ >= 0 && bufs_[i].fb_id) {
            drmModeRmFB(fd_, bufs_[i].fb_id);
            bufs_[i].fb_id = 0;
        }
        if (fd_ >= 0 && bufs_[i].handle) {
            drm_mode_destroy_dumb destroy{};
            destroy.handle = bufs_[i].handle;
            ::ioctl(fd_, DRM_IOCTL_MODE_DESTROY_DUMB, &destroy);
            bufs_[i].handle = 0;
        }
    }

    if (conn_) {
        drmModeFreeConnector(conn_);
        conn_ = nullptr;
    }
    if (resources_) {
        drmModeFreeResources(resources_);
        resources_ = nullptr;
    }
    if (fd_ >= 0) {
        ::close(fd_);
        fd_ = -1;
    }

    conn_id_ = 0;
    crtc_id_ = 0;
    scr_w_ = 0;
    scr_h_ = 0;
    pitch_ = 0;
    pitch_u32_ = 0;
    map_size_ = 0;
    back_idx_ = 0;
    front_idx_ = 0;
    flip_err_logged_ = false;
}

DrmScreen::DrmScreen() : impl_(std::make_unique<Impl>()) {}

DrmScreen::~DrmScreen() = default;

DrmScreen::DrmScreen(DrmScreen &&other) noexcept = default;

DrmScreen &DrmScreen::operator=(DrmScreen &&other) noexcept = default;

bool DrmScreen::init(char const *device_path) {
    return impl_->init(device_path);
}

void DrmScreen::setup_layout(unsigned rgb_w, unsigned rgb_h) {
    impl_->setup_layout(rgb_w, rgb_h);
}

void DrmScreen::present_lane(unsigned lane,
                             void const *src,
                             uint32_t pixfmt,
                             unsigned src_w,
                             unsigned src_h) {
    impl_->present_lane(lane, src, pixfmt, src_w, src_h);
}

void DrmScreen::present_ir_lr(void const *left,
                              void const *right,
                              uint32_t pixfmt,
                              unsigned eye_w,
                              unsigned eye_h) {
    impl_->present_ir_lr(left, right, pixfmt, eye_w, eye_h);
}

} // namespace uvc_tools
