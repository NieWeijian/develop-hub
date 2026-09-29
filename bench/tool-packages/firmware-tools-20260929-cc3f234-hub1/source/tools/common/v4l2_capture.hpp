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

#include <chrono>
#include <cstddef>
#include <cstdint>
#include <linux/videodev2.h>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

namespace uvc_tools {

class MappedBuffer {
    std::byte *data_ = nullptr;
    size_t size_ = 0;

  public:
    MappedBuffer() = default;
    MappedBuffer(std::byte *data, size_t size);
    ~MappedBuffer();

    MappedBuffer(MappedBuffer &&other) noexcept;
    MappedBuffer &operator=(MappedBuffer &&other) noexcept;
    MappedBuffer(MappedBuffer const &) = delete;
    MappedBuffer &operator=(MappedBuffer const &) = delete;

    explicit operator bool() const {
        return data_ != nullptr;
    }
    std::byte *data() const {
        return data_;
    }
    size_t size() const {
        return size_;
    }

  private:
    void reset();
};

struct CaptureFormat {
    uint32_t pixfmt = 0;
    int width = 0;
    int height = 0;
    int fps = 10;
};

std::optional<std::string> find_capture_device(std::string_view card_match);

/* Try several card-name matches in order (e.g. RMSL321_* then RMSL322_*).
 * The module's usb_config falls back to the RMSL322 name when the sensor
 * detection fails, so host tools must accept both. Takes owned strings:
 * an initializer_list<std::string_view> built from temporary std::strings
 * would dangle before the call. */
inline std::optional<std::string>
find_capture_device_any(std::vector<std::string> matches) {
    for (const auto &m : matches) {
        if (auto dev = find_capture_device(m))
            return dev;
    }
    return std::nullopt;
}

class V4l2Capture {
  public:
    V4l2Capture() = default;
    ~V4l2Capture();

    V4l2Capture(V4l2Capture &&other) noexcept;
    V4l2Capture &operator=(V4l2Capture &&other) noexcept;
    V4l2Capture(V4l2Capture const &) = delete;
    V4l2Capture &operator=(V4l2Capture const &) = delete;

    bool open(std::string_view device_path, bool nonblock = true);
    bool configure(CaptureFormat const &fmt, unsigned buffer_count = 4);
    bool configure_with_fallback(CaptureFormat const &fmt,
                                 std::vector<uint32_t> const &fallback_pixfmts,
                                 unsigned buffer_count = 4);
    /** Set format/fps only (no buffer alloc). Returns false if size mismatch. */
    bool try_format(uint32_t pixfmt, int width, int height, int fps);
    /** List discrete frame sizes the device advertises for a pixel format
     *  (VIDIOC_ENUM_FRAMESIZES). Empty when unknown/unsupported. */
    std::vector<std::pair<int, int>> enumerate_frame_sizes(uint32_t pixfmt) const;
    bool start();
    void stop();
    void close();

    bool poll_readable(std::chrono::milliseconds timeout) const;
    bool try_dequeue(struct v4l2_buffer &buf) const;
    void queue(struct v4l2_buffer const &buf) const;

    int fd() const;
    std::string const &device_path() const {
        return device_path_;
    }
    int width() const {
        return width_;
    }
    int height() const {
        return height_;
    }
    uint32_t pixfmt() const {
        return pixfmt_;
    }
    bool is_open() const;
    bool is_streaming() const {
        return streaming_;
    }
    MappedBuffer const &buffer(unsigned index) const {
        return buffers_.at(index);
    }

  private:
    class UniqueFd {
        int value_ = -1;

      public:
        UniqueFd() = default;
        explicit UniqueFd(int fd)
            : value_(fd) {}
        ~UniqueFd() {
            reset();
        }
        UniqueFd(UniqueFd &&o) noexcept
            : value_(o.release()) {}
        UniqueFd &operator=(UniqueFd &&o) noexcept {
            if (this != &o)
                reset(o.release());
            return *this;
        }
        UniqueFd(UniqueFd const &) = delete;
        UniqueFd &operator=(UniqueFd const &) = delete;
        explicit operator bool() const {
            return value_ >= 0;
        }
        int get() const {
            return value_;
        }
        int release() {
            int fd = value_;
            value_ = -1;
            return fd;
        }
        void reset(int fd = -1);
    };

    bool init_mmap_buffers(unsigned buffer_count);
    bool apply_format(uint32_t pixfmt, int width, int height, int fps);

    std::string device_path_;
    UniqueFd device_;
    int width_ = 0;
    int height_ = 0;
    uint32_t pixfmt_ = 0;
    bool streaming_ = false;
    std::vector<MappedBuffer> buffers_;
};

} // namespace uvc_tools
