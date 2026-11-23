#include "g1_unitree_control/mode5_rest_window.hpp"

#include <cstdlib>
#include <iostream>
#include <limits>

using Window = g1_task::Mode5RestWindow;

void require(bool condition, const char* explanation)
{
  if (!condition)
  {
    std::cerr << explanation << '\n';
    std::exit(1);
  }
}

int main()
{
  constexpr std::int64_t ms = 1000000;
  Window w;
  Window::Joints q{}, dq{};
  for (std::uint32_t i = 0; i <= 150; ++i)
  {
    q[0] = (i % 2) ? -.0001 : .0001;
    dq[0] = (i % 13) ? .009 : -.07;
    w.observe(i, 1 + ms * i, q, dq);
  }
  const auto still = w.evidence();
  require(still.ready() && still.samples == 151 && still.peak_raw_dq == .07,
          "stable position with short speed spikes should pass");
  w.observe(151, 1 + 151 * ms, q, dq);
  require(w.evidence().ready() && w.evidence().samples == 151, "window did not roll");
  w.observe(152, 1 + 182 * ms, q, dq);
  require(!w.evidence().ready() && w.evidence().samples == 1, "feedback gap did not reset window");
  require(w.continuity().gap_resets == 1, "feedback gap was not attributed");

  w.clear();
  q.fill(0.); dq.fill(.009);
  for (std::uint32_t i = 0; i <= 150; ++i)
  {
    q[0] = -.0001;
    dq[0] = .009;
    w.observe(i, 1 + ms * i, q, dq);
    if (i % 13 == 0)
    {
      q[0] = .0001;
      dq[0] = -.07;
      w.observe(i, 1 + ms * i + ms / 4, q, dq);
    }
  }
  const auto merged = w.evidence();
  require(merged.ready() && merged.samples == 151 && merged.span_ns == 150 * ms,
          "same-tick feedback variation should not erase continuity or add elapsed time");
  require(merged.peak_q_span == .0002 && merged.peak_raw_dq == .07 && w.continuity().merged_ticks == 12,
          "merged feedback must preserve the position and speed extrema");
  q[0] = .004;
  w.observe(150, 1 + 150 * ms + ms / 3, q, dq);
  require(!w.evidence().ready() && w.evidence().peak_q_span >= .004,
          "motion on a repeated tick should still block rest");
  q[0] = 0.;
  w.observe(150, 1 + 150 * ms + ms / 2, q, dq);
  require(!w.evidence().ready(), "a later same-tick frame must not hide the excursion");
  w.observe(151, 1 + 150 * ms, q, dq);
  require(w.evidence().samples == 1 && w.continuity().timestamp_resets == 1,
          "source-time reversal was not attributed");

  for (std::uint32_t scenario = 0; scenario < 3; ++scenario)
  {
    w.clear();
    q.fill(0.); dq.fill(0.);
    for (std::uint32_t i = 0; i <= 150; ++i)
    {
      if (scenario == 0) q[16] = i * .00004;
      if (scenario == 1) dq[3] = (i % 2) ? -.04 : .04;
      if (scenario == 2) dq[9] = i == 100 ? .151 : 0.;
      w.observe(i, 1 + ms * i, q, dq);
    }
    const auto moving = w.evidence();
    require(!moving.ready() && moving.span_ns == 150 * ms, "moving sample sequence passed");
  }

  w.clear();
  q.fill(0.); dq.fill(0.);
  for (std::uint32_t i = 0; i < 300; ++i) w.observe(100, 1 + ms * i, q, dq);
  require(!w.evidence().ready() && w.evidence().samples <= 1, "repeated tick counted as feedback");
  w.observe(101, 1 + 301 * ms, q, dq);
  w.observe(3, 1 + 302 * ms, q, dq);
  require(w.evidence().samples == 1 && w.continuity().tick_resets == 1,
          "reversed tick did not reset window");
  dq[16] = std::numeric_limits<double>::quiet_NaN();
  w.observe(4, 1 + 303 * ms, q, dq);
  require(w.evidence().samples == 0, "nonfinite sample did not clear window");
  return 0;
}
