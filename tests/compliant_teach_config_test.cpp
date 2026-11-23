#include "compliant_teach_config.hpp"

#include <cmath>
#include <iostream>

int main()
{
    const auto config = LoadCompliantTeachConfig("config/compliant_teach.yaml");
    if (config.profile_steps.count(config.default_profile) != 1 ||
        config.controller.filter_samples == 0 || config.controller.engage_samples == 0 ||
        config.maximum_current_mA != 300 || config.maximum_temperature_C != 60) {
        std::cerr << "Unexpected migrated profile or safety thresholds\n";
        return 1;
    }

    auto left = ApplyJointOverride(config.controller, config, "left", "thumb-bend");
    const auto right = ApplyJointOverride(config.controller, config, "right", "thumb-bend");
    if (left.close_delta != 80 || right.close_delta != 80 ||
        !left.opposite_force_releases || !right.opposite_force_releases) {
        std::cerr << "Missing migrated thumb overrides\n";
        return 1;
    }

    left.step = config.profile_steps.at("slow");
    CompliantTeach teach(100, 0.5f, left);
    const auto samples = left.filter_samples + left.engage_samples - 1;
    for (std::size_t i = 1; i <= samples; ++i) {
        const auto update = teach.ObserveForce(0);
        if (update.command.has_value() != (i == samples)) {
            std::cerr << "Unexpected force engagement timing\n";
            return 1;
        }
    }
    const auto settling = teach.ObserveForce(0);
    if (std::abs(teach.target() - (0.5f - left.step)) > 1e-6f ||
        !settling.ignored_dynamic_force || settling.command) {
        std::cerr << "Unexpected step or motion settling behavior\n";
        return 1;
    }
}
