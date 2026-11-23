#include "rh56/wch_port.hpp"

#include <iostream>
#include <unistd.h>

namespace {

namespace fs = std::filesystem;

void SetupPort(const fs::path& tty_class, const std::string& tty, const fs::path& usb,
               const std::string& serial, const std::string& product, const std::string& number)
{
    const auto interface = usb / number;
    fs::create_directories(interface);
    std::ofstream(usb / "idVendor") << "1a86\n";
    std::ofstream(usb / "idProduct") << product << '\n';
    std::ofstream(usb / "serial") << serial << '\n';
    std::ofstream(interface / "bInterfaceNumber") << number << '\n';
    fs::create_directories(tty_class / tty);
    fs::create_directory_symlink(interface, tty_class / tty / "device");
}

void Check(bool condition, const char* message)
{
    if (!condition)
        throw std::runtime_error(message);
}

void ExpectRejection(const fs::path& tty_class, const char* selector, const char* reason)
{
    try {
        rh56::ResolveWchPort(selector, tty_class);
    } catch (const std::exception& error) {
        Check(std::string(error.what()).find(reason) != std::string::npos,
              "Port was rejected with an unexpected error");
        return;
    }
    throw std::runtime_error("Unexpected port match for " + std::string(selector));
}

}  // namespace

int main()
{
    const auto root = fs::current_path() / ("wch-port-test-" + std::to_string(getpid()));
    if (!fs::create_directory(root)) {
        std::cerr << "Test directory already exists: " << root << '\n';
        return 1;
    }
    bool passed = false;
    try {
        const auto tty_class = root / "tty";
        SetupPort(tty_class, "ttyCH343USB8", root / "usb-a", "board-A", "55ec", "00");
        SetupPort(tty_class, "ttyCH343USB3", root / "usb-a", "board-A", "55ec", "02");
        SetupPort(tty_class, "ttyCH343USB0", root / "usb-b", "board-B", "55ec", "00");
        SetupPort(tty_class, "ttyCH343USB1", root / "usb-c", "board-A", "55e7", "00");
        SetupPort(tty_class, "ttyACM2", root / "usb-d", "board-A", "55ec", "00");
        Check(rh56::ResolveWchPort("board-A:00", tty_class) == "/dev/ttyCH343USB8", "Wrong port for if00");
        Check(rh56::ResolveWchPort("board-A:02", tty_class) == "/dev/ttyCH343USB3", "Wrong port for if02");
        fs::rename(tty_class / "ttyCH343USB8", tty_class / "ttyCH343USB9");
        Check(rh56::ResolveWchPort("board-A:00", tty_class) == "/dev/ttyCH343USB9", "Selection used stale tty number");
        ExpectRejection(tty_class, "board-A:01", "Expected WCH port");
        ExpectRejection(tty_class, "../board-A:00", "Expected WCH port");
        ExpectRejection(tty_class, "board-B:02", "No WCH");
        SetupPort(tty_class, "ttyCH343USB4", root / "usb-e", "board-A", "55ec", "00");
        ExpectRejection(tty_class, "board-A:00", "Multiple WCH");
        passed = true;
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
    }
    fs::remove_all(root);
    return passed ? 0 : 1;
}
