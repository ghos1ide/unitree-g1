#pragma once

#include <algorithm>
#include <array>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <deque>

namespace g1_task {

class Mode5RestWindow
{
public:
  using Joints = std::array<double, 17>;
  static constexpr char method[] = "q_span_and_dq_v2";
  static constexpr std::int64_t window_ns = 150000000;
  static constexpr std::int64_t max_gap_ns = 30000000;
  static constexpr std::size_t min_samples = 8;
  static constexpr double max_span_rad = .003;
  static constexpr double max_mean_abs_dq = .03;
  static constexpr double max_raw_dq = .15;

  struct Continuity
  {
    std::size_t merged_ticks = 0;
    std::size_t gap_resets = 0;
    std::size_t timestamp_resets = 0;
    std::size_t tick_resets = 0;
    std::size_t invalid_samples = 0;
    std::int64_t longest_span_ns = 0;
  };

  struct Evidence
  {
    std::int64_t span_ns = 0;
    std::size_t samples = 0;
    double peak_q_span = 0.;
    double peak_mean_abs_dq = 0.;
    double peak_raw_dq = 0.;

    bool ready() const
    {
      return span_ns >= window_ns && samples >= min_samples && peak_q_span <= max_span_rad &&
        peak_mean_abs_dq <= max_mean_abs_dq && peak_raw_dq <= max_raw_dq;
    }
  };

  void clear() { samples_.clear(); }
  const Continuity& continuity() const { return continuity_; }

  void observe(std::uint32_t tick, std::int64_t stamp_ns, const Joints& q, const Joints& dq)
  {
    if (stamp_ns <= 0) { ++continuity_.invalid_samples; clear(); return; }
    Joints abs_dq;
    for (std::size_t i = 0; i < q.size(); ++i)
    {
      if (!std::isfinite(q[i]) || !std::isfinite(dq[i]))
      {
        ++continuity_.invalid_samples;
        clear();
        return;
      }
      abs_dq[i] = std::abs(dq[i]);
    }
    if (!samples_.empty())
    {
      auto& last = samples_.back();
      if (stamp_ns < last.latest_ns || (tick != last.tick && stamp_ns == last.latest_ns))
      {
        ++continuity_.timestamp_resets;
        clear();
      }
      else if (stamp_ns - last.stamp_ns > max_gap_ns)
      {
        ++continuity_.gap_resets;
        clear();
      }
      else if (tick < last.tick)
      {
        ++continuity_.tick_resets;
        clear();
      }
      else if (tick == last.tick)
      {
        // Several DDS publications can share one robot tick. Count elapsed time
        // only once, but preserve every observed position and speed extremum.
        ++continuity_.merged_ticks;
        last.latest_ns = stamp_ns;
        for (std::size_t i = 0; i < q.size(); ++i)
        {
          last.q_low[i] = std::min(last.q_low[i], q[i]);
          last.q_high[i] = std::max(last.q_high[i], q[i]);
          last.abs_dq[i] = std::max(last.abs_dq[i], abs_dq[i]);
        }
        return;
      }
    }
    while (samples_.size() >= 2 && stamp_ns - samples_[1].stamp_ns >= window_ns)
      samples_.pop_front();
    if (samples_.size() >= 512) samples_.pop_front();
    samples_.push_back({tick, stamp_ns, stamp_ns, q, q, abs_dq});
    continuity_.longest_span_ns = std::max(continuity_.longest_span_ns, stamp_ns - samples_.front().stamp_ns);
  }

  Evidence evidence() const
  {
    Evidence result;
    if (samples_.empty()) return result;
    result.samples = samples_.size();
    result.span_ns = samples_.back().stamp_ns - samples_.front().stamp_ns;
    auto low = samples_.front().q_low, high = samples_.front().q_high;
    Joints summed_dq{};
    for (std::size_t s = 0; s < samples_.size(); ++s)
    {
      const auto& sample = samples_[s];
      for (std::size_t i = 0; i < low.size(); ++i)
      {
        low[i] = std::min(low[i], sample.q_low[i]);
        high[i] = std::max(high[i], sample.q_high[i]);
        result.peak_raw_dq = std::max(result.peak_raw_dq, sample.abs_dq[i]);
        if (s)
        {
          const auto& prev = samples_[s - 1];
          summed_dq[i] += .5 * (sample.abs_dq[i] + prev.abs_dq[i]) * (sample.stamp_ns - prev.stamp_ns);
        }
      }
    }
    for (std::size_t i = 0; i < low.size(); ++i)
    {
      result.peak_q_span = std::max(result.peak_q_span, high[i] - low[i]);
      if (result.span_ns)
        result.peak_mean_abs_dq = std::max(result.peak_mean_abs_dq, summed_dq[i] / result.span_ns);
    }
    return result;
  }

private:
  struct Sample
  {
    std::uint32_t tick;
    std::int64_t stamp_ns;
    std::int64_t latest_ns;
    Joints q_low;
    Joints q_high;
    Joints abs_dq;
  };
  std::deque<Sample> samples_;
  Continuity continuity_;
};

}  // namespace g1_task
