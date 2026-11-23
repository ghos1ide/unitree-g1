#!/usr/bin/env bash
# Read the body model ID from live LowState. Does not command the robot.
set -e

if [[ ${1:-} == --help ]]; then
    printf 'Usage: bash %s [robot_network_interface] [--sources|--snapshot]\nDefault interface: eth0\n' "$0"
    exit 0
fi
iface=${1:-eth0}
mode=${2:-}
if (( $# > 2 )) || [[ ! $iface =~ ^[[:alnum:]_.:-]+$ || ( -n $mode && $mode != --sources && $mode != --snapshot ) ]]; then
    printf 'Expected an interface name (default: eth0), optionally followed by --sources or --snapshot.\n' >&2
    exit 2
fi

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
sdk=/home/unitree/unitree_sdk2-main
dds_lib=$sdk/thirdparty/lib/aarch64
test -f "$dds_lib/libddsc.so"
export CYCLONEDDS_URI="<CycloneDDS><Domain><General><Interfaces><NetworkInterface name=\"$iface\"/></Interfaces></General><Discovery><ParticipantIndex>auto</ParticipantIndex><MaxAutoParticipantIndex>80</MaxAutoParticipantIndex></Discovery></Domain></CycloneDDS>"

if [[ -n $mode ]]; then
    mkdir -p "$here/../../build"
    binary=$(mktemp "$here/../../build/.g1-model-sources.XXXXXXXX")
    trap 'rm -f -- "$binary"' EXIT
    g++ -std=c++17 -O2 -Wall -Wextra -pthread "$here/read_model_sources.cpp" \
        -I"$here/../action_runtime_v3/native_ws/src/g1_unitree_control/include" \
        -isystem "$sdk/include" -isystem "$sdk/thirdparty/include" \
        -isystem "$sdk/thirdparty/include/ddscxx" "$sdk/lib/aarch64/libunitree_sdk2.a" \
        -L"$dds_lib" -Wl,--disable-new-dtags,-rpath,"$dds_lib" -lddscxx -lddsc -o "$binary"
    capture_timeout=10s
    if [[ $mode == --snapshot ]]; then capture_timeout=25s; fi
    LD_LIBRARY_PATH="$dds_lib:${LD_LIBRARY_PATH:-}" timeout "$capture_timeout" "$binary" "$mode"
    exit
fi

runtime=$here/../action_runtime_v3
source /opt/ros/foxy/setup.bash
source "$runtime/native_ws/install/local_setup.bash"
export LD_LIBRARY_PATH="$dds_lib:${LD_LIBRARY_PATH:-}"
export ROS_DOMAIN_ID=0 ROS_LOCALHOST_ONLY=0 RMW_IMPLEMENTATION=rmw_cyclonedds_cpp

exec timeout 10s /usr/bin/python3 -B - "$iface" <<'PY'
import sys
import time

import rclpy
from rclpy.qos import qos_profile_sensor_data
from unitree_hg.msg import LowState

topic = '/lowstate'
modes, ticks = set(), set()


def on_state(msg):
    modes.add(msg.mode_machine)
    ticks.add(msg.tick)


rclpy.init(args=[])
node = None
try:
    node = rclpy.create_node('g1_model_readonly', enable_rosout=False, start_parameter_services=False)
    node.create_subscription(LowState, topic, on_state, qos_profile_sensor_data)
    deadline = time.monotonic() + 5.
    while rclpy.ok() and time.monotonic() < deadline:
        rclpy.spin_once(node, timeout_sec=.1)
    publishers = node.count_publishers(topic)
    if publishers > 1:
        for endpoint in node.get_publishers_info_by_topic(topic):
            name = endpoint.node_namespace.rstrip('/') + '/' + endpoint.node_name
            gid = bytes(endpoint.endpoint_gid).hex()
            print(f'{topic} publisher: node={name}, type={endpoint.topic_type}, gid={gid}', file=sys.stderr)
    if publishers > 1 or len(ticks) < 3 or len(modes) != 1 or 0 in modes:
        sys.exit(f'No unambiguous live model ID on {sys.argv[1]}: '
                 f'publishers={publishers}, ticks={len(ticks)}, observed_modes={sorted(modes)}')
    print(f'mode_machine={next(iter(modes))}')
except Exception as error:
    sys.exit(f'Cannot read {topic} on {sys.argv[1]}: {error}')
finally:
    if node is not None:
        node.destroy_node()
    rclpy.shutdown()
PY
