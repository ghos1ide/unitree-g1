// Attribute LowState samples to their DDS writers without a control publisher.
#include <dds/dds.h>
#include <dds/dds.hpp>
#include <unitree/common/json/json.hpp>
#include <unitree/idl/hg/LowState_.hpp>
#include "g1_unitree_control/mode5_rest_window.hpp"

#include <algorithm>
#include <array>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <iomanip>
#include <iostream>
#include <limits>
#include <map>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>

namespace {
constexpr char kTopic[] = "rt/lowstate";
constexpr std::array<unsigned int, 3> kWaistSlots{12, 13, 14};
constexpr auto kMaxAge = std::chrono::milliseconds(100);
using Clock = std::chrono::steady_clock;
using RestWindow = g1_task::Mode5RestWindow;
using MotorState = unitree_hg::msg::dds_::MotorState_;
using LowState = unitree_hg::msg::dds_::LowState_;
using unitree::common::JsonArray;
using unitree::common::JsonMap;

struct Peer {
    std::string host, process, pid;
};

struct WaistFeedback {
    std::set<unsigned int> modes;
    size_t finite_frames = 0, invalid_frames = 0;
    float q_min = 0, q_max = 0, peak_abs_dq = 0, vol_min = 0, vol_max = 0;

    void observe(const MotorState& motor) {
        modes.insert(motor.mode());
        const float q = motor.q(), dq = motor.dq(), vol = motor.vol();
        if (!std::isfinite(q) || !std::isfinite(dq) || !std::isfinite(vol)) {
            ++invalid_frames;
            return;
        }
        if (finite_frames++ == 0) {
            q_min = q_max = q;
            vol_min = vol_max = vol;
        } else {
            q_min = std::min(q_min, q);
            q_max = std::max(q_max, q);
            vol_min = std::min(vol_min, vol);
            vol_max = std::max(vol_max, vol);
        }
        peak_abs_dq = std::max(peak_abs_dq, std::abs(dq));
    }
};

struct Snapshot {
    std::array<MotorState, 17> joints;
    RestWindow rest;
    Clock::time_point received{};
    std::uint32_t last_tick = 0;
    std::array<size_t, 17> speed_exceeded_frames{};
    size_t unhealthy_frames = 0, stale_tick_frames = 0;

    void observe(const LowState& state, std::int64_t stamp, Clock::time_point now) {
        bool healthy = state.mode_machine() == 5 && state.mode_pr() == 0;
        RestWindow::Joints q, dq;
        for (size_t i = 0; i < joints.size(); ++i) {
            const auto& joint = state.motor_state()[12 + i];
            joints[i] = joint;
            q[i] = joint.q();
            dq[i] = joint.dq();
            if (std::isfinite(dq[i]) && std::abs(dq[i]) > RestWindow::max_mean_abs_dq)
                ++speed_exceeded_frames[i];
            healthy = healthy && joint.mode() == 1 && joint.motorstate() == 0 &&
                std::abs(double(joint.tau_est())) <= 25. && std::isfinite(joint.vol()) && joint.vol() > 1 &&
                std::max(joint.temperature()[0], joint.temperature()[1]) < 85;
        }
        if (!healthy) {
            ++unhealthy_frames;
            rest.clear();
        }
        if (received != Clock::time_point{} && now - received > kMaxAge) {
            ++stale_tick_frames;
            rest.clear();
        }
        if (healthy) rest.observe(state.tick(), stamp, q, dq);
        if (received == Clock::time_point{} || state.tick() != last_tick) received = now;
        last_tick = state.tick();
    }
};

struct Source {
    std::string participant;
    std::set<unsigned int> models, modes_pr;
    std::set<uint32_t> ticks;
    std::array<WaistFeedback, kWaistSlots.size()> waist;
    Snapshot snapshot;
    size_t frames = 0;
};

bool ready_snapshot(const std::map<std::string, Source>& sources,
                    const std::map<std::string, Peer>& participants, size_t unattributed,
                    Clock::time_point now) {
    if (unattributed) return false;
    const Source* active = nullptr;
    for (const auto& entry : sources) {
        if (!entry.second.frames) continue;
        if (active) return false;
        active = &entry.second;
    }
    if (!active || active->models != std::set<unsigned int>{5} ||
        active->modes_pr != std::set<unsigned int>{0}) return false;
    const auto peer = participants.find(active->participant);
    if (peer == participants.end() || peer->second.host != "Unitree" || peer->second.process != "basic_service")
        return false;
    const auto& snapshot = active->snapshot;
    return now - snapshot.received <= kMaxAge && snapshot.rest.evidence().ready();
}

std::string guid(const dds_guid_t& key) {
    std::ostringstream out;
    for (auto byte : key.v) out << std::hex << std::setw(2) << std::setfill('0') << unsigned(byte);
    return out.str();
}

std::string prop(const dds_qos_t* qos, const char* key) {
    char* value = nullptr;
    if (!qos || !dds_qget_prop(qos, key, &value) || !value) return "unknown";
    std::string result(value);
    dds_free(value);
    return result;
}

template<class T, class Callback>
void drain(dds_entity_t reader, Callback handle) {
    void* samples[64]{};
    dds_sample_info_t info[64];
    const auto count = dds_take(reader, samples, info, 64, 64);
    if (count < 0) throw std::runtime_error("DDS discovery read failed");
    for (int i = 0; i < count; ++i) {
        if (info[i].valid_data) handle(*static_cast<const T*>(samples[i]));
    }
    if (count) dds_return_loan(reader, samples, count);
}

std::string values(const std::set<unsigned int>& set) {
    std::string out;
    for (auto v : set) out += (out.empty() ? "" : ",") + std::to_string(v);
    return '[' + out + ']';
}

JsonMap source_json(const std::string& id, const Source& source, const Peer& peer) {
    const auto& snapshot = source.snapshot;
    const auto window = snapshot.rest.evidence();
    const auto& continuity = snapshot.rest.continuity();
    JsonArray joints;
    if (source.frames) {
        for (size_t i = 0; i < snapshot.joints.size(); ++i) {
            const auto& motor = snapshot.joints[i];
            JsonMap row{{"slot", unsigned(12 + i)}, {"mode", unsigned(motor.mode())},
                {"speed_exceeded_frames", uint64_t(snapshot.speed_exceeded_frames[i])},
                {"fault", motor.motorstate()}, {"max_temperature_c", int(std::max(motor.temperature()[0], motor.temperature()[1]))}};
            for (const auto& field : {std::pair{"q", motor.q()}, std::pair{"dq", motor.dq()},
                                      std::pair{"vol", motor.vol()}, std::pair{"tau_est", motor.tau_est()}}) {
                if (std::isfinite(field.second)) row[field.first] = double(field.second);
            }
            joints.push_back(row);
        }
    }
    return {{"writer", id}, {"participant", source.participant}, {"host", peer.host}, {"process", peer.process},
        {"frames", uint64_t(source.frames)}, {"mode_machine", JsonArray(source.models.begin(), source.models.end())},
        {"mode_pr", JsonArray(source.modes_pr.begin(), source.modes_pr.end())}, {"tick", snapshot.last_tick},
        {"stationary_s", window.span_ns * 1e-9}, {"stationary_ticks", uint64_t(window.samples)},
        {"rest_window", JsonMap{{"method", std::string(RestWindow::method)}, {"passed", window.ready()},
            {"q_span_rad", window.peak_q_span}, {"mean_abs_dq_rad_s", window.peak_mean_abs_dq},
            {"peak_abs_dq_rad_s", window.peak_raw_dq}}},
        {"window_continuity", JsonMap{{"distinct_observed_ticks", uint64_t(source.ticks.size())},
            {"merged_ticks", uint64_t(continuity.merged_ticks)}, {"gap_resets", uint64_t(continuity.gap_resets)},
            {"timestamp_resets", uint64_t(continuity.timestamp_resets)}, {"tick_resets", uint64_t(continuity.tick_resets)},
            {"unhealthy_frames", uint64_t(snapshot.unhealthy_frames)}, {"stale_tick_frames", uint64_t(snapshot.stale_tick_frames)},
            {"invalid_samples", uint64_t(continuity.invalid_samples)}, {"longest_window_s", continuity.longest_span_ns * 1e-9}}},
        {"age_s", source.frames ? std::chrono::duration<double>(Clock::now() - snapshot.received).count() : -1.},
        {"joints", joints}};
}
}  // namespace

int main(int argc, char** argv) {
    if (argc != 2 || (std::string(argv[1]) != "--sources" && std::string(argv[1]) != "--snapshot")) return 2;
    const bool snapshot_mode = std::string(argv[1]) == "--snapshot";
    try {
        dds::domain::DomainParticipant participant(0);
        const auto native = participant.delegate()->get_ddsc_entity();
        const auto peers = dds_create_reader(native, DDS_BUILTIN_TOPIC_DCPSPARTICIPANT, nullptr, nullptr);
        const auto pubs = dds_create_reader(native, DDS_BUILTIN_TOPIC_DCPSPUBLICATION, nullptr, nullptr);
        if (peers < 0 || pubs < 0) throw std::runtime_error("Cannot subscribe to DDS discovery");
        dds::topic::Topic<LowState> topic(participant, kTopic);
        dds::sub::Subscriber subscriber(participant);
        auto qos = subscriber.default_datareader_qos();
        qos << dds::core::policy::Reliability::BestEffort() << dds::core::policy::History::KeepLast(256);
        dds::sub::DataReader<LowState> reader(subscriber, topic, qos);

        std::map<std::string, Peer> participants;
        std::map<std::string, Source> sources;
        size_t unattributed = 0;
        const auto discovery_end = Clock::now() + std::chrono::seconds(5);
        const auto end = discovery_end + std::chrono::seconds(snapshot_mode ? 15 : 0);
        while (std::chrono::steady_clock::now() < end) {
            drain<dds_builtintopic_participant_t>(peers, [&](const auto& p) {
                participants[guid(p.key)] = {prop(p.qos, "__Hostname"),
                                            prop(p.qos, "__ProcessName"), prop(p.qos, "__Pid")};
            });
            drain<dds_builtintopic_endpoint_t>(pubs, [&](const auto& p) {
                if (p.topic_name && std::string(p.topic_name) == kTopic) {
                    sources[guid(p.key)].participant = guid(p.participant_key);
                }
            });
            for (const auto& sample : reader.take()) {
                if (!sample.info().valid()) continue;
                auto* p = dds_get_matched_publication_data(reader.delegate()->get_ddsc_entity(),
                    sample.info().publication_handle().delegate().handle());
                if (!p) { ++unattributed; continue; }
                auto& source = sources[guid(p->key)];
                source.participant = guid(p->participant_key);
                dds_builtintopic_free_endpoint(p);
                const auto& data = sample.data();
                ++source.frames;
                source.ticks.insert(data.tick());
                source.models.insert(data.mode_machine());
                source.modes_pr.insert(data.mode_pr());
                for (size_t i = 0; i < kWaistSlots.size(); ++i) {
                    source.waist[i].observe(data.motor_state()[kWaistSlots[i]]);
                }
                if (snapshot_mode) {
                    const auto stamp = sample.info().timestamp();
                    const auto sec = stamp.sec();
                    const std::int64_t ns = sec >= 0 && sec < std::numeric_limits<std::int64_t>::max() / 1000000000
                        ? sec * 1000000000 + stamp.nanosec() : 0;
                    source.snapshot.observe(data, ns, Clock::now());
                }
            }
            const auto now = Clock::now();
            if (snapshot_mode && now >= discovery_end && ready_snapshot(sources, participants, unattributed, now))
                break;
            std::this_thread::sleep_for(std::chrono::milliseconds(snapshot_mode ? 10 : 50));
        }

        size_t received = 0;
        JsonArray snapshots;
        for (const auto& item : sources) {
            const auto& source = item.second;
            received += source.frames;
            const auto p = participants.find(source.participant);
            const auto peer = p == participants.end() ? Peer{"unknown", "unknown", "unknown"} : p->second;
            if (snapshot_mode) {
                snapshots.push_back(source_json(item.first, source, peer));
                continue;
            }
            std::cout << "writer=" << item.first << " participant=" << source.participant
                << " host=" << std::quoted(peer.host) << " process=" << std::quoted(peer.process)
                << " pid=" << std::quoted(peer.pid) << " frames=" << source.frames
                << " ticks=" << source.ticks.size() << " mode_machine=" << values(source.models)
                << " mode_pr=" << values(source.modes_pr) << '\n';
            if (!source.frames) continue;
            for (size_t i = 0; i < kWaistSlots.size(); ++i) {
                const auto& axis = source.waist[i];
                std::cout << "waist_feedback: writer=" << item.first << " slot=" << kWaistSlots[i]
                    << " motor_mode=" << values(axis.modes) << " finite_frames=" << axis.finite_frames
                    << " invalid_frames=" << axis.invalid_frames;
                if (axis.finite_frames) {
                    std::cout << " q_range=[" << axis.q_min << ',' << axis.q_max << ']'
                        << " peak_abs_dq=" << axis.peak_abs_dq
                        << " vol_range=[" << axis.vol_min << ',' << axis.vol_max << ']';
                }
                std::cout << '\n';
            }
        }
        if (snapshot_mode) {
            std::cout << unitree::common::ToJsonString(JsonMap{{"schema", 1}, {"sources", snapshots},
                {"unattributed_frames", uint64_t(unattributed)}}) << '\n';
            return received ? 0 : 1;
        }
        std::cout << "observed_writers=" << sources.size() << " unattributed_frames=" << unattributed << '\n';
        return received ? 0 : 1;
    } catch (const std::exception& error) {
        std::cerr << "Cannot observe LowState sources: " << error.what() << '\n';
        return 1;
    }
}
