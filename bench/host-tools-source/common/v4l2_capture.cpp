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

#include "v4l2_capture.hpp"

#include <cerrno>
#include <fcntl.h>
#include <filesystem>
#include <fstream>
#include <poll.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <system_error>
#include <thread>
#include <unistd.h>

namespace fs = std::filesystem;

namespace uvc_tools {
namespace {

void v4l2_ioctl(int fd, unsigned long request, void *arg) {
    while (::ioctl(fd, request, arg) < 0) {
        if (errno != EINTR)
            throw std::system_error(errno, std::generic_category(), "v4l2 ioctl failed");
    }
}

std::optional<std::string> read_sysfs_name(fs::path const &path) {
    std::ifstream input(path);
    if (!input)
        return std::nullopt;

    std::string name;
    if (!std::getline(input, name))
        return std::nullopt;

    while (!name.empty() && (name.back() == '\r' || name.back() == '\n'))
        name.pop_back();
    return name;
}

bool is_capture_device(int fd) {
    v4l2_capability cap{};
    if (::ioctl(fd, VIDIOC_QUERYCAP, &cap) < 0)
        return false;
    uint32_t const caps = cap.device_caps ? cap.device_caps : cap.capabilities;
    return (caps & V4L2_CAP_VIDEO_CAPTURE) && (caps & V4L2_CAP_STREAMING);
}

} // namespace

MappedBuffer::MappedBuffer(std::byte *data, size_t size)
    : data_(data),
      size_(size) {}

MappedBuffer::~MappedBuffer() {
    reset();
}

MappedBuffer::MappedBuffer(MappedBuffer &&other) noexcept
    : data_(other.data_),
      size_(other.size_) {
    other.data_ = nullptr;
    other.size_ = 0;
}

MappedBuffer &MappedBuffer::operator=(MappedBuffer &&other) noexcept {
    if (this != &other) {
        reset();
        data_ = other.data_;
        size_ = other.size_;
        other.data_ = nullptr;
        other.size_ = 0;
    }
    return *this;
}

void MappedBuffer::reset() {
    if (data_ != nullptr) {
        ::munmap(data_, size_);
        data_ = nullptr;
        size_ = 0;
    }
}

void V4l2Capture::UniqueFd::reset(int fd) {
    if (value_ >= 0)
        ::close(value_);
    value_ = fd;
}

std::optional<std::string> find_capture_device_once(std::string_view card_match) {
    fs::path const root = "/sys/class/video4linux";
    if (!fs::exists(root))
        return std::nullopt;

    for (auto const &entry : fs::directory_iterator(root)) {
        std::string const node = entry.path().filename().string();
        if (node.rfind("video", 0) != 0)
            continue;

        auto const name = read_sysfs_name(entry.path() / "name");
        if (!name || name->find(card_match) == std::string::npos)
            continue;

        std::string const candidate = "/dev/" + node;
        int probe_fd = ::open(candidate.c_str(), O_RDWR | O_NONBLOCK | O_CLOEXEC);
        if (probe_fd < 0)
            continue;

        bool ok = is_capture_device(probe_fd);
        ::close(probe_fd);
        if (ok)
            return candidate;
    }
    return std::nullopt;
}

std::optional<std::string> find_capture_device(std::string_view card_match) {
    /* The module can re-enumerate (video nodes renumber) under load; retry
     * briefly so transient discovery gaps do not fail the caller. */
    for (int attempt = 0; attempt < 10; ++attempt) {
        if (attempt > 0)
            std::this_thread::sleep_for(std::chrono::milliseconds(300));
        if (auto dev = find_capture_device_once(card_match))
            return dev;
    }
    return std::nullopt;
}

V4l2Capture::~V4l2Capture() {
    close();
}

V4l2Capture::V4l2Capture(V4l2Capture &&other) noexcept
    : device_path_(std::move(other.device_path_)),
      device_(other.device_.release()),
      width_(other.width_),
      height_(other.height_),
      pixfmt_(other.pixfmt_),
      streaming_(other.streaming_),
      buffers_(std::move(other.buffers_)) {
    other.streaming_ = false;
}

V4l2Capture &V4l2Capture::operator=(V4l2Capture &&other) noexcept {
    if (this != &other) {
        close();
        device_path_ = std::move(other.device_path_);
        device_.reset(other.device_.release());
        width_ = other.width_;
        height_ = other.height_;
        pixfmt_ = other.pixfmt_;
        streaming_ = other.streaming_;
        buffers_ = std::move(other.buffers_);
        other.streaming_ = false;
    }
    return *this;
}

bool V4l2Capture::open(std::string_view device_path, bool nonblock) {
    close();
    device_path_ = std::string(device_path);
    int flags = O_RDWR | O_CLOEXEC;
    if (nonblock)
        flags |= O_NONBLOCK;
    device_.reset(::open(device_path_.c_str(), flags));
    return static_cast<bool>(device_);
}

bool V4l2Capture::apply_format(uint32_t pixfmt, int width, int height, int fps) {
    v4l2_format vfmt{};
    vfmt.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    vfmt.fmt.pix.width = static_cast<uint32_t>(width);
    vfmt.fmt.pix.height = static_cast<uint32_t>(height);
    vfmt.fmt.pix.pixelformat = pixfmt;
    vfmt.fmt.pix.field = V4L2_FIELD_NONE;
    v4l2_ioctl(device_.get(), VIDIOC_S_FMT, &vfmt);

    if (static_cast<int>(vfmt.fmt.pix.width) != width ||
        static_cast<int>(vfmt.fmt.pix.height) != height)
        return false;

    width_ = static_cast<int>(vfmt.fmt.pix.width);
    height_ = static_cast<int>(vfmt.fmt.pix.height);
    pixfmt_ = vfmt.fmt.pix.pixelformat;

    v4l2_streamparm parm{};
    parm.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    if (::ioctl(device_.get(), VIDIOC_G_PARM, &parm) == 0 &&
        (parm.parm.capture.capability & V4L2_CAP_TIMEPERFRAME)) {
        parm.parm.capture.timeperframe.numerator = 1;
        parm.parm.capture.timeperframe.denominator = static_cast<uint32_t>(fps);
        v4l2_ioctl(device_.get(), VIDIOC_S_PARM, &parm);
    }
    return true;
}

bool V4l2Capture::configure(CaptureFormat const &fmt, unsigned buffer_count) {
    if (!device_)
        return false;

    try {
        buffers_.clear();
        if (!apply_format(fmt.pixfmt, fmt.width, fmt.height, fmt.fps))
            return false;
        return init_mmap_buffers(buffer_count);
    } catch (std::system_error const &) {
        return false;
    }
}

bool V4l2Capture::configure_with_fallback(CaptureFormat const &fmt,
                                          std::vector<uint32_t> const &fallback_pixfmts,
                                          unsigned buffer_count) {
    if (!device_)
        return false;

    auto try_pix = [&](uint32_t pix) {
        buffers_.clear();
        if (!apply_format(pix, fmt.width, fmt.height, fmt.fps))
            return false;
        return init_mmap_buffers(buffer_count);
    };

    try {
        if (try_pix(fmt.pixfmt))
            return true;
        for (uint32_t pix : fallback_pixfmts) {
            if (pix != fmt.pixfmt && try_pix(pix))
                return true;
        }
    } catch (std::system_error const &) {
        return false;
    }
    return false;
}

bool V4l2Capture::try_format(uint32_t pixfmt, int width, int height, int fps) {
    if (!device_)
        return false;
    try {
        return apply_format(pixfmt, width, height, fps);
    } catch (std::system_error const &) {
        return false;
    }
}

std::vector<std::pair<int, int>> V4l2Capture::enumerate_frame_sizes(uint32_t pixfmt) const {
    std::vector<std::pair<int, int>> sizes;
    if (!device_)
        return sizes;
    for (unsigned i = 0;; ++i) {
        v4l2_frmsizeenum frmsize{};
        frmsize.index = i;
        frmsize.pixel_format = pixfmt;
        if (::ioctl(device_.get(), VIDIOC_ENUM_FRAMESIZES, &frmsize) < 0)
            break;
        if (frmsize.type == V4L2_FRMSIZE_TYPE_DISCRETE) {
            sizes.emplace_back(static_cast<int>(frmsize.discrete.width),
                               static_cast<int>(frmsize.discrete.height));
        } else if (frmsize.type == V4L2_FRMSIZE_TYPE_STEPWISE ||
                   frmsize.type == V4L2_FRMSIZE_TYPE_CONTINUOUS) {
            sizes.emplace_back(static_cast<int>(frmsize.stepwise.max_width),
                               static_cast<int>(frmsize.stepwise.max_height));
            break;
        }
    }
    return sizes;
}

bool V4l2Capture::init_mmap_buffers(unsigned buffer_count) {
    buffers_.clear();

    v4l2_requestbuffers req{};
    req.count = buffer_count;
    req.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    req.memory = V4L2_MEMORY_MMAP;
    v4l2_ioctl(device_.get(), VIDIOC_REQBUFS, &req);
    if (req.count < 2)
        return false;

    buffers_.resize(req.count);
    for (unsigned i = 0; i < req.count; ++i) {
        v4l2_buffer buf{};
        buf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        buf.memory = V4L2_MEMORY_MMAP;
        buf.index = i;
        v4l2_ioctl(device_.get(), VIDIOC_QUERYBUF, &buf);

        auto *mapped = static_cast<std::byte *>(::mmap(
            nullptr, buf.length, PROT_READ | PROT_WRITE, MAP_SHARED, device_.get(), buf.m.offset));
        if (mapped == MAP_FAILED)
            throw std::system_error(errno, std::generic_category(), "mmap failed");

        buffers_[i] = MappedBuffer(mapped, buf.length);
        v4l2_ioctl(device_.get(), VIDIOC_QBUF, &buf);
    }
    return true;
}

bool V4l2Capture::start() {
    if (!device_ || streaming_)
        return false;
    try {
        v4l2_buf_type type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        v4l2_ioctl(device_.get(), VIDIOC_STREAMON, &type);
        streaming_ = true;
        return true;
    } catch (std::system_error const &) {
        return false;
    }
}

void V4l2Capture::stop() {
    if (!device_ || !streaming_)
        return;
    v4l2_buf_type type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    ::ioctl(device_.get(), VIDIOC_STREAMOFF, &type);
    streaming_ = false;
}

void V4l2Capture::close() {
    stop();
    buffers_.clear();
    device_.reset();
    device_path_.clear();
    width_ = 0;
    height_ = 0;
    pixfmt_ = 0;
}

bool V4l2Capture::poll_readable(std::chrono::milliseconds timeout) const {
    if (!device_)
        return false;
    pollfd pfd{device_.get(), POLLIN, 0};
    int const rc = ::poll(&pfd, 1, static_cast<int>(timeout.count()));
    if (rc < 0)
        return errno == EINTR;
    return rc > 0 && (pfd.revents & POLLIN);
}

bool V4l2Capture::try_dequeue(v4l2_buffer &buf) const {
    if (!device_)
        return false;
    buf = {};
    buf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    buf.memory = V4L2_MEMORY_MMAP;
    if (::ioctl(device_.get(), VIDIOC_DQBUF, &buf) < 0) {
        if (errno == EAGAIN)
            return false;
        throw std::system_error(errno, std::generic_category(), "VIDIOC_DQBUF failed");
    }
    return true;
}

void V4l2Capture::queue(v4l2_buffer const &buf) const {
    v4l2_buffer copy = buf;
    v4l2_ioctl(device_.get(), VIDIOC_QBUF, &copy);
}

int V4l2Capture::fd() const {
    return device_.get();
}

bool V4l2Capture::is_open() const {
    return static_cast<bool>(device_);
}

} // namespace uvc_tools
