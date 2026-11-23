#pragma once

#include <algorithm>
#include <cctype>
#include <filesystem>
#include <fstream>
#include <stdexcept>
#include <string>

namespace rh56 {

inline std::string ReadUsbAttribute(const std::filesystem::path& path)
{
    std::ifstream input(path);
    std::string value;
    input >> value;
    return value;
}

inline std::filesystem::path ResolveWchPort(
    const std::string& selector,
    const std::filesystem::path& tty_class = "/sys/class/tty")
{
    namespace fs = std::filesystem;
    constexpr char prefix[] = "ttyCH343USB";
    const auto separator = selector.find(':');
    if (separator == std::string::npos || separator == 0)
        throw std::invalid_argument("Expected WCH port SERIAL:00 or SERIAL:02");
    const auto serial = selector.substr(0, separator);
    const auto interface_number = selector.substr(separator + 1);
    if ((interface_number != "00" && interface_number != "02") ||
        !std::all_of(serial.begin(), serial.end(), [](unsigned char c) {
            return std::isalnum(c) || c == '_' || c == '-';
        }))
        throw std::invalid_argument("Expected WCH port SERIAL:00 or SERIAL:02");

    fs::path match;
    for (const auto& entry : fs::directory_iterator(tty_class)) {
        const auto tty = entry.path().filename().string();
        constexpr std::size_t prefix_size = sizeof(prefix) - 1;
        if (tty.compare(0, prefix_size, prefix) != 0 || tty.size() <= prefix_size ||
            !std::all_of(tty.begin() + prefix_size, tty.end(), [](unsigned char c) {
                return std::isdigit(c);
            }))
            continue;
        std::error_code error;
        const auto interface = fs::canonical(entry.path() / "device", error);
        if (error || ReadUsbAttribute(interface / "bInterfaceNumber") != interface_number)
            continue;
        const auto usb = interface.parent_path();
        if (ReadUsbAttribute(usb / "idVendor") != "1a86" ||
            ReadUsbAttribute(usb / "idProduct") != "55ec" ||
            ReadUsbAttribute(usb / "serial") != serial)
            continue;
        if (!match.empty())
            throw std::runtime_error("Multiple WCH serial devices match " + selector);
        match = fs::path("/dev") / tty;
    }
    if (match.empty())
        throw std::runtime_error("No WCH 1a86:55ec serial device matches " + selector);
    return match;
}

}  // namespace rh56
