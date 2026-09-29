/*
 * USB device helpers for multi-module host tools.
 *
 * With several RMSL321 modules on one hub every /dev/videoN and /dev/hidrawN
 * is ambiguous, so tools accept an explicit primary video node and derive the
 * sibling nodes (DEPTH / IR / HID) from the USB device that owns it.
 */
#pragma once

#include <cerrno>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <optional>
#include <string>
#include <string_view>

#include <fcntl.h>
#include <linux/videodev2.h>
#include <sys/ioctl.h>
#include <unistd.h>

namespace uvc_tools {

namespace fs = std::filesystem;

inline std::string read_sysfs_text(fs::path const &p) {
    std::ifstream f(p);
    std::string s;
    std::getline(f, s);
    while (!s.empty() && (s.back() == '\n' || s.back() == '\r' || s.back() == ' '))
        s.pop_back();
    return s;
}

inline bool video_node_is_capture(std::string const &dev) {
    int fd = ::open(dev.c_str(), O_RDWR | O_NONBLOCK | O_CLOEXEC);
    if (fd < 0)
        return false;
    v4l2_capability cap{};
    bool ok = ::ioctl(fd, VIDIOC_QUERYCAP, &cap) == 0;
    ::close(fd);
    if (!ok)
        return false;
    uint32_t const caps = cap.device_caps ? cap.device_caps : cap.capabilities;
    return (caps & V4L2_CAP_VIDEO_CAPTURE) && (caps & V4L2_CAP_STREAMING);
}

/* Walk up from a video node to the owning USB device sysfs dir
 * (/sys/bus/usb/devices/<port>); empty when not USB. */
inline std::string usb_devpath_for_video(std::string const &video_dev) {
    fs::path const node = fs::path(video_dev).filename();
    std::error_code ec;
    fs::path const dev =
        fs::weakly_canonical(fs::path("/sys/class/video4linux") / node / "device", ec);
    if (ec)
        return {};
    for (fs::path p = dev; !p.empty(); p = p.parent_path()) {
        if (fs::exists(p / "idVendor", ec) && fs::exists(p / "idProduct", ec))
            return p.string();
    }
    return {};
}

/* Hub port path (e.g. "2-1.1") used by dfu-util --path. */
inline std::string usb_port_path(std::string const &video_dev) {
    std::string const usb = usb_devpath_for_video(video_dev);
    if (usb.empty())
        return {};
    fs::path const p(usb);
    return p.filename().string();
}

/* The capture video node under the same USB device whose card name contains
 * name_match (e.g. "RMSL321_DEPTH"). */
inline std::optional<std::string> sibling_video_node(std::string const &video_dev,
                                                     std::string_view name_match) {
    std::string const usb = usb_devpath_for_video(video_dev);
    if (usb.empty())
        return std::nullopt;
    std::error_code ec;
    for (auto const &entry : fs::recursive_directory_iterator(usb, ec)) {
        std::string const leaf = entry.path().filename().string();
        if (leaf.rfind("video", 0) != 0)
            continue;
        std::string const name = read_sysfs_text(entry.path() / "name");
        if (name.find(name_match) == std::string::npos)
            continue;
        std::string const dev = "/dev/" + leaf;
        if (video_node_is_capture(dev))
            return dev;
    }
    return std::nullopt;
}

/* First /dev/hidrawN under the same USB device (module IMU HID). */
inline std::optional<std::string> hidraw_for_video(std::string const &video_dev) {
    std::string const usb = usb_devpath_for_video(video_dev);
    if (usb.empty())
        return std::nullopt;
    std::error_code ec;
    for (auto const &entry : fs::recursive_directory_iterator(usb, ec)) {
        std::string const leaf = entry.path().filename().string();
        if (leaf.rfind("hidraw", 0) != 0)
            continue;
        return "/dev/" + leaf;
    }
    return std::nullopt;
}

} // namespace uvc_tools
