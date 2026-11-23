#!/usr/bin/env bash
set -e
source "$(dirname -- "${BASH_SOURCE[0]}")/ros2_env.sh"
exec "${ELN_PYTHON_EXECUTABLE:-$DEMO_ROOT/.venv/bin/python}" -m robot_control_bridge.fake_action_server
