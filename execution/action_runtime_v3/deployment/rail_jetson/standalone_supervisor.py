"""Start the standalone native/API stack. Never automatically ARM or initialize.

On shutdown retain an armed gateway for physical support. Use the explicit
release procedure before terminating that remaining controller.
"""
import argparse
from contextlib import closing, contextmanager
import fcntl
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import time
import traceback
from urllib.request import build_opener, ProxyHandler, Request

from bundle import ROOT, write
from releases import load_release
from dds_authority import DdsAuthority, arm_sdk_evidence


SDK_ROOT = Path('/home/unitree/unitree_sdk2-main')


def publishers(node):
    result = {}
    for topic in ('/arm_sdk', '/g1/arm_gateway/diagnostics'):
        endpoints = node.get_publishers_info_by_topic(topic)
        result[topic] = [dict(node=info.node_name, namespace=info.node_namespace,
                              gid=bytes(info.endpoint_gid).hex()) for info in endpoints]
    return result


def print_authority_evidence(authority):
    evidence = arm_sdk_evidence(authority.snapshot()['discovery'])
    print('ARM SDK DDS evidence: ' + json.dumps(evidence, sort_keys=True), flush=True)


def print_authority_ready(authority, native, mode5):
    if mode5:
        reader = authority.snapshot()['pinned_reader']
        print('Mode 5 DDS ownership clear: Unitree/humanoid reader %s, no /arm_sdk writers.' % reader,
              flush=True)
    else:
        print('Identified idle onboard SDK endpoints: ' + json.dumps(sorted(native)), flush=True)


def inspect_arm_sdk(root, *, mode5=False):
    # The observer subscribes to discovery and /arm_sdk; it has no command writer.
    authority = DdsAuthority(root, mode5=mode5)
    try:
        deadline = time.monotonic() + 6.
        while authority.process.poll() is None and time.monotonic() < deadline:
            if authority.snapshot()['discovery'].get('observed_seconds', 0.) >= 4.:
                break
            time.sleep(.1)
        try:
            native = authority.check()
        except RuntimeError as error:
            print_authority_evidence(authority)
            print('DDS ownership blocked: ' + str(error), file=sys.stderr, flush=True)
            return 2
        print_authority_evidence(authority)
        print_authority_ready(authority, native, mode5)
        return 0
    finally:
        authority.close()


def inspect_right_hand():
    import rclpy
    from ros_transport import jetson_hand

    try:
        rclpy.init()
        node = None
        try:
            node = rclpy.create_node('jetson_right_hand_check')
            deadline = time.monotonic() + 3.
            while time.monotonic() < deadline:
                rclpy.spin_once(node, timeout_sec=.1)
            discovery = dict(request_readers=node.count_subscribers('/api/rh56_hand/request'),
                             response_writers=node.count_publishers('/api/rh56_hand/response'),
                             inspire_command_writers=node.count_publishers('/inspire/cmd'))
            print('RH56 DDS discovery: ' + json.dumps(discovery, sort_keys=True), flush=True)
        finally:
            if node is not None:
                node.destroy_node()
            rclpy.shutdown()
        if discovery['response_writers'] > 1:
            raise RuntimeError('Multiple RH56 RPC response writers; resolve service ownership first')
        with closing(jetson_hand('eth0', SDK_ROOT)) as hand:
            state = hand.read('right')
        print('Right hand feedback ready: ' + json.dumps(state, sort_keys=True), flush=True)
        return 0
    except (OSError, RuntimeError, ValueError) as error:
        print('Right hand check failed: ' + str(error), file=sys.stderr, flush=True)
        return 2


def output_may_be_armed(state):
    # Missing diagnostics are unknown, not evidence of released support.
    return state.get('native', {}).get('gateway', {}).get('armed') != 'false'


def runtime_parameters(root, data=None, mode5=False):
    # Foxy can give node-specific file values precedence over CLI -p overrides.
    # Authorize this explicit launch in one file, leaving selected defaults inert.
    config = json.loads((root / 'config/native.yaml').read_text())
    parameters = config['g1_arm_sdk_hardware_gateway']['ros__parameters']
    parameters.update(
        hardware_output_enabled=True, hardware_authorization='I_UNDERSTAND_ARM_SDK_OUTPUT')
    if mode5:
        from mode5_locked import update_gateway_parameters
        update_gateway_parameters(parameters)
        parameters['trajectory_log_directory'] = str(data / 'native_motion_logs')
    path = (data or root / 'data') / 'runtime_gateway.yaml'
    write(path, config)
    return path


def local_credentials(data):
    data.mkdir(mode=0o700, exist_ok=True)
    for name in ('native.token', 'api.key'):
        path = data / name
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            if path.is_symlink() or path.stat().st_mode & 0o077:
                raise ValueError('Mode 5 credentials must be local files with permissions 0600')
        else:
            with os.fdopen(fd, 'w') as stream:
                stream.write(secrets.token_urlsafe(32) + '\n')
    return data / 'native.token', data / 'api.key'


def wait_ready(read_state, children, label, timeout=60., stopping=lambda: False):
    deadline = time.monotonic() + timeout
    last_error = 'no response'
    while time.monotonic() < deadline:
        if stopping():
            raise RuntimeError('Startup interrupted')
        for name, child in children:
            code = child.poll()
            if code is not None:
                raise RuntimeError('%s exited (code=%s); inspect %s.log' % (name, code, name))
        try:
            return read_state()
        except OSError as error:
            last_error = str(error)
            time.sleep(.2)
    raise TimeoutError('%s unavailable after %.0fs: %s' % (label, timeout, last_error))


@contextmanager
def arm_owner_locks(root):
    with (root / 'data/runtime.lock').open('a') as owner:
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with Path('/tmp/g1-arm-sdk-owner-%s.lock' % os.getuid()).open('a') as machine:
            fcntl.flock(machine, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument('--hardware', action='store_true')
    operation.add_argument('--inspect-arm-sdk', action='store_true',
                           help='Read DDS identities and command frame counts; start no controller or API')
    operation.add_argument('--inspect-mode5-arm-sdk', action='store_true',
                           help='Check the Mode 5 arm SDK reader and absence of command writers')
    operation.add_argument('--inspect-robot-services', action='store_true',
                           help='Query the read-only onboard Robot ServiceList (API 1003)')
    operation.add_argument('--inspect-right-hand', action='store_true',
                           help='Check RH56 DDS endpoints and read only the right-hand feedback')
    operation.add_argument('--mode5-arm-service', choices=('stop', 'restore'),
                           help='Explicitly stop or restore only the onboard g1_arm_example service')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, help='Default: 8795 (legacy) or 18795 (Mode 5)')
    parser.add_argument('--relay-port', type=int, default=0, help='Optional relay; 8090 is normally owned by eln_interface.service')
    parser.add_argument('--check-only', action='store_true', help='Check ownership and Mode 5 ServiceList; launch nothing')
    parser.add_argument('--mode5-preflight', type=Path, help='Passing live preflight.json for locked-waist pull')
    parser.add_argument('--confirm-mode5-clear', action='store_true',
                        help='Confirm reviewed path clearance, waist calibration, robot support and physical stop')
    args = parser.parse_args()
    if os.environ.get('ROS_DOMAIN_ID') != '0' or os.environ.get('ROS_LOCALHOST_ONLY') != '0':
        raise RuntimeError('Use run_standalone.sh for the explicit robot network environment')
    root = ROOT
    launch_options = (args.check_only or args.mode5_preflight or args.relay_port or
                      args.port is not None or args.host != '127.0.0.1')
    if args.inspect_arm_sdk or args.inspect_mode5_arm_sdk or args.inspect_robot_services or args.inspect_right_hand:
        if launch_options or args.confirm_mode5_clear:
            parser.error('Inspection takes no launch options')
        if args.inspect_right_hand:
            return inspect_right_hand()
        if args.inspect_robot_services:
            from inspect_robot_services import inspect_services
            return inspect_services()
        return inspect_arm_sdk(root, mode5=args.inspect_mode5_arm_sdk)
    if args.mode5_arm_service:
        if launch_options or not args.confirm_mode5_clear:
            parser.error('--mode5-arm-service takes only --confirm-mode5-clear for stationary support and physical stop')
        from mode5_arm_handoff import handoff
        try:
            with arm_owner_locks(root):
                return handoff(root, enable=args.mode5_arm_service == 'restore')
        except BlockingIOError:
            parser.error('A local arm runtime is still present; release output and exit it first')
    selected = root / 'config/selected_bundle.json'
    release = load_release(selected)
    mode5 = args.mode5_preflight is not None
    args.port = args.port if args.port is not None else (18795 if mode5 else 8795)
    data = root / 'data'
    if args.confirm_mode5_clear and not mode5:
        parser.error('--confirm-mode5-clear requires --mode5-preflight')
    if mode5:
        from mode5_locked import BINARY, DATA, LockedPreparation
        if args.host != '127.0.0.1' or args.relay_port:
            parser.error('Mode 5 commissioning is loopback-only, without a relay')
        if not args.check_only and not args.confirm_mode5_clear:
            parser.error('Review the preview and verify support, clearance, calibration and stop before confirming')
        args.mode5_preflight = args.mode5_preflight.resolve()
        try:
            LockedPreparation(args.mode5_preflight, release, root)
        except (OSError, KeyError, TypeError, ValueError) as error:
            parser.error('Mode 5 preflight rejected before DDS startup: ' + str(error))
        data = root / DATA
        if not args.check_only:
            for binary in (root / BINARY, root / 'build/task_hand_client'):
                if not os.access(binary, os.X_OK):
                    parser.error('Missing executable: ' + str(binary))
    with arm_owner_locks(root):
        return run_runtime(args, root, selected, release, data, mode5)


def run_runtime(args, root, selected, release, data, mode5):
    # Refuse a pre-existing ROS gateway or writer before launching ours.
    import rclpy
    rclpy.init()
    probe = rclpy.create_node('jetson_standalone_start_check')
    authority = None
    try:
        authority = DdsAuthority(root, mode5=mode5)
        deadline = time.monotonic() + 4.
        while time.monotonic() < deadline:
            rclpy.spin_once(probe, timeout_sec=.1)
        if mode5:
            from inspect_robot_services import RobotServices
            from mode5_arm_handoff import require_service_released
            try:
                require_service_released(RobotServices(probe).service_list())
            except (RuntimeError, ValueError) as error:
                print('Mode 5 ServiceList blocked: ' + str(error), file=sys.stderr, flush=True)
                return 2
        occupied = publishers(probe)
        print('Controller discovery: ' + json.dumps(occupied), flush=True)
        require_idle = args.check_only or mode5
        try:
            native = authority.check() if require_idle else set()
        except RuntimeError as error:
            print_authority_evidence(authority)
            print('DDS ownership blocked: ' + str(error), file=sys.stderr, flush=True)
            return 2
        external = [p for p in occupied['/arm_sdk'] if p['gid'][:32] not in native]
        if require_idle and (external or occupied['/g1/arm_gateway/diagnostics']):
            print_authority_evidence(authority)
            raise RuntimeError('Another controller is present; no takeover attempted. '
                               'Disarming or closing a window is not process shutdown. '
                               'Publisher identities are printed above.')
        if require_idle:
            print_authority_ready(authority, native, mode5)
        else:
            print('Startup is passive; output ownership will be checked on initialize.', flush=True)
    finally:
        if authority:
            authority.close()
        probe.destroy_node()
        rclpy.shutdown()
    if args.check_only:
        print('Preflight clear; no controller or API started, no motion sent.', flush=True)
        return
    gateway_root = root.parents[1] / 'communication/eln_http_demo'
    token_file, key_file = local_credentials(data) if mode5 else (root / 'config/native.token', root / 'config/api.key')
    token = token_file.read_text().strip()
    opener = build_opener(ProxyHandler({}))
    logs = data / 'service_logs'
    logs.mkdir(parents=True, exist_ok=True)
    children, streams, named_children = [], [], []
    stopping = [False]
    def signal_stop(*unused):
        stopping[0] = True
    signal.signal(signal.SIGINT, signal_stop)
    signal.signal(signal.SIGTERM, signal_stop)
    def spawn(name, command):
        stream = (logs / (name + '.log')).open('a')
        streams.append(stream)
        process = subprocess.Popen(command, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
        children.append(process)
        named_children.append((name, process))
        return process
    def state():
        request = Request('http://127.0.0.1:8794/state', data=b'{}', headers={'X-Native-Token': token, 'Content-Type': 'application/json'})
        with opener.open(request, timeout=3.) as response:
            return json.load(response)
    native = None
    armed = False
    try:
        runtime_parameters(root, data, mode5)
        spawn('worker', ['/usr/bin/python3', str(root / 'deployment/rail_jetson/native_worker.py'),
            '--hardware', '--managed-gateway', '--release', str(selected), '--urdf', str(root / 'assets/planner.urdf'),
            '--sdk', str(SDK_ROOT), '--token-file', str(token_file), '--journal', str(data / 'native_jobs.json')] +
            (['--mode5-preflight', str(args.mode5_preflight)] if mode5 else []))
        wait_ready(state, named_children, 'Native worker', stopping=lambda: stopping[0])
        spawn('api', [str(gateway_root / '.venv/bin/python'), str(root / 'deployment/rail_jetson/production_api.py'),
            '--gateway-root', str(gateway_root), '--release', str(selected), '--database', str(data / 'production.sqlite3'),
            '--native-token-file', str(token_file), '--api-key-file', str(key_file),
            '--host', args.host, '--port', str(args.port)] + (['--mode5-locked'] if mode5 else []))
        def api_state():
            host = '127.0.0.1' if args.host in ('0.0.0.0', '127.0.0.1', 'localhost') else args.host
            with opener.open('http://%s:%s/health' % (host, args.port), timeout=3.) as response:
                result = json.load(response)
            if (result.get('backend') != 'recorded-native' or result.get('simulation') is not False
                    or result.get('releaseSha256') != release['sha256']):
                raise RuntimeError('HTTP backend/mode/release mismatch; no initialization attempted')
            return result
        wait_ready(api_state, named_children, 'Production HTTP API', stopping=lambda: stopping[0])
        if args.relay_port:
            spawn('relay', [str(gateway_root / '.venv/bin/python'), str(root / 'deployment/rail_jetson/http_relay.py'),
                           '--host', args.host, '--port', str(args.relay_port),
                           '--upstream', 'http://127.0.0.1:' + str(args.port)])
        def relay_ready():
            host = '127.0.0.1' if args.host == '0.0.0.0' else args.host
            with opener.open('http://%s:%s/health' % (host, args.relay_port), timeout=3.) as response:
                result = json.load(response)
            if result.get('backend') != 'recorded-native' or result.get('simulation') is not False:
                raise RuntimeError('Relay did not reach the real executor')
            return result
        if args.relay_port:
            wait_ready(relay_ready, named_children, 'Real HTTP relay', stopping=lambda: stopping[0])
        print('Native Jetson HTTP READY; controller DETACHED until initialize, no trajectory sent.', flush=True)
        print('HTTP http://%s:%s; actions=%s' % (args.host, args.port,
              'pull' if mode5 else ','.join(release['payload']['actions'])), flush=True)
        while not stopping[0]:
            if any(p.poll() is not None for p in children):
                raise RuntimeError('A stack process exited; stopping task admission')
            try:
                armed = output_may_be_armed(state())
            except OSError:
                armed = True
            time.sleep(.5)
    except BaseException:
        # Print before cleanup, which can intentionally retain an armed gateway.
        traceback.print_exc(file=sys.stderr)
        print('Failure logs: ' + str(logs), file=sys.stderr, flush=True)
        raise
    finally:
        try:
            armed = output_may_be_armed(state())
        except Exception:
            # Unknown output state must not cause loss of arm support.
            armed = native is not None and native.poll() is None
        for child in reversed(children):
            if child.poll() is None:
                child.send_signal(signal.SIGINT)
                try:
                    child.wait(timeout=40.)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
        if native and native.poll() is None:
            if armed:
                print('Controller support retained; native heartbeat will STOP motion. Do not kill the gateway while loaded.', flush=True)
                print('Gateway PID: %s. Unload/support the arms before the robot shutdown procedure.' % native.pid, flush=True)
                while native.poll() is None:
                    time.sleep(1.)
            else:
                native.send_signal(signal.SIGINT)
                native.wait(timeout=15.)
        for stream in streams:
            stream.close()


if __name__ == '__main__':
    raise SystemExit(main())
