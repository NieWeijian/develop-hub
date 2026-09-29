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

#pragma once

#include <cstddef>
#include <cstdint>
#include <memory>
#include <string>
#include <vector>

namespace uvc_tools {

struct PreviewFrame {
    std::vector<std::byte> payload;
    int width = 0;
    int height = 0;
    bool valid = false;
};

/** RGB + depth side-by-side preview (ioctl/plane path, RV1126 panel). */
class DrmPreviewDisplay {
public:
    DrmPreviewDisplay();
    ~DrmPreviewDisplay();

    DrmPreviewDisplay(DrmPreviewDisplay const &) = delete;
    DrmPreviewDisplay &operator=(DrmPreviewDisplay const &) = delete;
    DrmPreviewDisplay(DrmPreviewDisplay &&other) noexcept;
    DrmPreviewDisplay &operator=(DrmPreviewDisplay &&other) noexcept;

    bool init(std::string const &device_path =
                  "/dev/dri/by-path/platform-display-subsystem-card");
    bool present_composite(PreviewFrame const &rgb, PreviewFrame const &depth);
    bool present_composite_ir(PreviewFrame const &rgb, PreviewFrame const &ir);
    bool present_composite_all(PreviewFrame const &rgb,
                               PreviewFrame const &depth,
                               PreviewFrame const &ir);

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

/** Single-lane fullscreen preview and IR left/right (libdrm SetCrtc path). */
class DrmScreen {
public:
    DrmScreen();
    ~DrmScreen();

    DrmScreen(DrmScreen const &) = delete;
    DrmScreen &operator=(DrmScreen const &) = delete;
    DrmScreen(DrmScreen &&other) noexcept;
    DrmScreen &operator=(DrmScreen &&other) noexcept;

    bool init(char const *device_path = "/dev/dri/card0");
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

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

} // namespace uvc_tools
