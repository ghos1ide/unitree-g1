  TrajectorySample interpolate_locked(double elapsed) const
  {
    const auto count = active_arm_indices_.size();
    TrajectorySample result;
    result.positions.assign(count, 0.0);
    result.velocities.assign(count, 0.0);
    result.accelerations.assign(count, 0.0);
    if (trajectory_start_blend_s_ > 0.0 && elapsed < trajectory_start_blend_s_)
    {
      const double u = std::clamp(elapsed / trajectory_start_blend_s_, 0.0, 1.0);
      const double u2 = u * u;
      const double u3 = u2 * u;
      const double u4 = u3 * u;
      const double u5 = u4 * u;
      const double blend = 10.0 * u3 - 15.0 * u4 + 6.0 * u5;
      const double blend_velocity = (30.0 * u2 - 60.0 * u3 + 30.0 * u4) /
        trajectory_start_blend_s_;
      const double blend_acceleration = (60.0 * u - 180.0 * u2 + 120.0 * u3) /
        (trajectory_start_blend_s_ * trajectory_start_blend_s_);
      for (std::size_t j = 0; j < count; ++j)
      {
        const double delta =
          trajectory_.points.front().positions[j] - trajectory_start_positions_[j];
        result.positions[j] = trajectory_start_positions_[j] + blend * delta;
        result.velocities[j] = blend_velocity * delta;
        result.accelerations[j] = blend_acceleration * delta;
      }
      return result;
    }

    const double trajectory_elapsed = trajectory_first_point_time_s_ +
      std::max(0.0, elapsed - trajectory_start_blend_s_);
    if (trajectory_elapsed >= point_time(trajectory_.points.back()))
    {
      std::copy_n(
        trajectory_.points.back().positions.begin(), count, result.positions.begin());
      return result;
    }
    std::size_t upper = 0;
    while (upper < trajectory_.points.size() &&
      point_time(trajectory_.points[upper]) < trajectory_elapsed)
      ++upper;
    const double lower_time = upper == 0 ? 0.0 : point_time(trajectory_.points[upper - 1]);
    const double upper_time = point_time(trajectory_.points[upper]);
    const double dt = upper_time - lower_time;
    const double ratio = dt > 0.0 ?
      std::clamp((trajectory_elapsed - lower_time) / dt, 0.0, 1.0) : 1.0;
    for (std::size_t j = 0; j < count; ++j)
    {
      const double lower = upper == 0 ? trajectory_start_positions_[j] :
        trajectory_.points[upper - 1].positions[j];
      const double upper_position = trajectory_.points[upper].positions[j];
      if (upper > 0 && trajectory_.points[upper - 1].velocities.size() == count &&
          trajectory_.points[upper].velocities.size() == count)
      {
        const auto& lower_point = trajectory_.points[upper - 1];
        const auto& upper_point = trajectory_.points[upper];
        const double u2 = ratio * ratio;
        const double u3 = u2 * ratio;
        if (lower_point.accelerations.size() == count &&
            upper_point.accelerations.size() == count)
        {
          const double u4 = u3 * ratio;
          const double u5 = u4 * ratio;
          const double h00 = 1.0 - 10.0 * u3 + 15.0 * u4 - 6.0 * u5;
          const double h10 = ratio - 6.0 * u3 + 8.0 * u4 - 3.0 * u5;
          const double h20 = 0.5 * u2 - 1.5 * u3 + 1.5 * u4 - 0.5 * u5;
          const double h01 = 10.0 * u3 - 15.0 * u4 + 6.0 * u5;
          const double h11 = -4.0 * u3 + 7.0 * u4 - 3.0 * u5;
          const double h21 = 0.5 * u3 - u4 + 0.5 * u5;
          const double dh00 = -30.0 * u2 + 60.0 * u3 - 30.0 * u4;
          const double dh10 = 1.0 - 18.0 * u2 + 32.0 * u3 - 15.0 * u4;
          const double dh20 = ratio - 4.5 * u2 + 6.0 * u3 - 2.5 * u4;
          const double dh01 = 30.0 * u2 - 60.0 * u3 + 30.0 * u4;
          const double dh11 = -12.0 * u2 + 28.0 * u3 - 15.0 * u4;
          const double dh21 = 1.5 * u2 - 4.0 * u3 + 2.5 * u4;
          const double ddh00 = -60.0 * ratio + 180.0 * u2 - 120.0 * u3;
          const double ddh10 = -36.0 * ratio + 96.0 * u2 - 60.0 * u3;
          const double ddh20 = 1.0 - 9.0 * ratio + 18.0 * u2 - 10.0 * u3;
          const double ddh01 = 60.0 * ratio - 180.0 * u2 + 120.0 * u3;
          const double ddh11 = -24.0 * ratio + 84.0 * u2 - 60.0 * u3;
          const double ddh21 = 3.0 * ratio - 12.0 * u2 + 10.0 * u3;
          const double lower_velocity = lower_point.velocities[j];
          const double upper_velocity = upper_point.velocities[j];
          const double lower_acceleration = lower_point.accelerations[j];
          const double upper_acceleration = upper_point.accelerations[j];
          result.positions[j] =
            h00 * lower + h10 * dt * lower_velocity +
            h20 * dt * dt * lower_acceleration + h01 * upper_position +
            h11 * dt * upper_velocity + h21 * dt * dt * upper_acceleration;
          result.velocities[j] =
            dh00 * lower / dt + dh10 * lower_velocity +
            dh20 * dt * lower_acceleration + dh01 * upper_position / dt +
            dh11 * upper_velocity + dh21 * dt * upper_acceleration;
          result.accelerations[j] =
            ddh00 * lower / (dt * dt) + ddh10 * lower_velocity / dt +
            ddh20 * lower_acceleration + ddh01 * upper_position / (dt * dt) +
            ddh11 * upper_velocity / dt + ddh21 * upper_acceleration;
        }
        else
        {
          const double h00 = 2.0 * u3 - 3.0 * u2 + 1.0;
          const double h10 = u3 - 2.0 * u2 + ratio;
          const double h01 = -2.0 * u3 + 3.0 * u2;
          const double h11 = u3 - u2;
          const double dh00 = 6.0 * u2 - 6.0 * ratio;
          const double dh10 = 3.0 * u2 - 4.0 * ratio + 1.0;
          const double dh01 = -6.0 * u2 + 6.0 * ratio;
          const double dh11 = 3.0 * u2 - 2.0 * ratio;
          const double ddh00 = 12.0 * ratio - 6.0;
          const double ddh10 = 6.0 * ratio - 4.0;
          const double ddh01 = -12.0 * ratio + 6.0;
          const double ddh11 = 6.0 * ratio - 2.0;
          const double lower_velocity = lower_point.velocities[j];
          const double upper_velocity = upper_point.velocities[j];
          result.positions[j] = h00 * lower + h10 * dt * lower_velocity +
            h01 * upper_position + h11 * dt * upper_velocity;
          result.velocities[j] = dh00 * lower / dt + dh10 * lower_velocity +
            dh01 * upper_position / dt + dh11 * upper_velocity;
          result.accelerations[j] = ddh00 * lower / (dt * dt) +
            ddh10 * lower_velocity / dt + ddh01 * upper_position / (dt * dt) +
            ddh11 * upper_velocity / dt;
        }
      }
      else
      {
        result.positions[j] = lower + ratio * (upper_position - lower);
        result.velocities[j] = dt > 0.0 ? (upper_position - lower) / dt : 0.0;
      }
    }
    return result;
  }

