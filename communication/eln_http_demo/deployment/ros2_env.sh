#!/usr/bin/env bash
# Foxy extension modules require the robot's existing Python 3.8 environment.
DEMO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
source /opt/ros/foxy/setup.bash
source "$DEMO_ROOT/ros2_ws/install/setup.bash"
export ROS_DOMAIN_ID=73
export ROS_LOCALHOST_ONLY=1
export RMW_IMPLEMENTATION=rmw_fastrtps_cpp
export ROS_ACTION_NAME=/eln_demo/robot_task
export PYTHONUNBUFFERED=1
cd "$DEMO_ROOT"
