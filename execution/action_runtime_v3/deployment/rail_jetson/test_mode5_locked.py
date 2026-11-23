"""Offline admission checks; no DDS or controller is started."""
from contextlib import nullcontext, redirect_stderr, redirect_stdout
import copy
import csv
import hashlib
import io
import json
import math
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from bundle import ROOT, digest
from mode5_locked import (ACTIVE_SERVICES, HOLD_TOL, INACTIVE_SERVICES, MODEL_SHA, NEUTRAL,
                          REST_METHOD, START_TOL, LockedPreparation, update_gateway_parameters)
from native_worker import Worker, main as worker_main
from releases import load_release
from robot_request import main as request_main
from ros_transport import RosTransport
from standalone_supervisor import run_runtime


class LockedPreparationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.release = load_release(ROOT / 'config/selected_bundle.json')

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / 'preflight.json'
        self.preview = self.path.with_name('locked_alignment_preview.csv')
        self.target = dict(self.release['payload']['initialization']['target'], **NEUTRAL)
        self.start = dict(self.target, waist_roll_joint=.05, waist_pitch_joint=-.04)
        self.report = dict(action='pull', release_sha256=self.release['sha256'], model_sha256=MODEL_SHA,
                           capture='live', read_only=True, hardware_executable=False, hardware_commands_sent=0,
                           inspect_only=False, feedback_problems={}, stationary_window_passed=True,
                           stationary_s=.151, stationary_ticks=35, age_at_capture_s=.05, matches={'locked': {}},
                           rest_window=dict(method=REST_METHOD, passed=True, q_span_rad=.0004,
                                            mean_abs_dq_rad_s=.015, peak_abs_dq_rad_s=.073))
        self.write_preview()

    def write_preview(self, duration_ns=2_000_000_000, target=None):
        suffixes = ('_rad', '_rad_s', '_rad_s2')
        with self.preview.open('w', newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow(['kind', 'time_ns'] + [n + s for s in suffixes for n in self.target])
            for time_ns, q in ((0, self.start), (duration_ns, target or self.target)):
                writer.writerow(['offline_alignment_preview', time_ns] + list(q.values()) + [0.] * (2 * len(q)))
        self.report['matches']['locked'].update(preview_generated=True, preview_file=self.preview.name,
            preview_sha256=hashlib.sha256(self.preview.read_bytes()).hexdigest(), duration_ns=duration_ns)

    def load(self):
        self.path.write_text(json.dumps(self.report))
        return LockedPreparation(self.path, self.release, ROOT)

    def test_two_point_target_preserves_frozen_pull(self):
        preparation = self.load()
        measured = dict(self.start, right_shoulder_pitch_joint=self.start['right_shoulder_pitch_joint'] + .01123,
                        waist_pitch_joint=self.start['waist_pitch_joint'] + .049)
        result = preparation.trajectory(measured)
        self.assertEqual(len(result['joint_names']), 17)
        self.assertEqual(len(result['points']), 2)
        self.assertEqual(dict(zip(result['joint_names'], result['points'][0]['positions'])), self.start)
        self.assertEqual(dict(zip(result['joint_names'], result['points'][-1]['positions'])), self.target)
        for step in preparation.action['program']:
            if step['kind'] == 'recorded':
                self.assertEqual(digest(step['trajectory']), step['trajectory_sha256'])
                self.assertFalse(set(NEUTRAL) & set(step['trajectory']['joint_names']))
        self.assertEqual([s['side'] for s in preparation.action['program'] if s['kind'] == 'hand'], ['right'] * 3)
        with self.assertRaisesRegex(RuntimeError, r'ARMED_HOLD; no preparation trajectory sent.*waist_roll_joint=\+0\.05100'):
            preparation.trajectory(dict(self.start, waist_roll_joint=.101))

    def test_start_comparison_reports_signed_drift_and_missing_feedback(self):
        preparation = self.load()
        self.assertTrue(preparation.compare_start(self.start)['matches'])
        measured = dict(self.start, waist_roll_joint=.105, waist_pitch_joint=-.102, waist_yaw_joint=float('nan'))
        measured.pop('left_elbow_joint')
        result = preparation.compare_start(measured)
        self.assertFalse(result['matches'])
        self.assertEqual(result['tolerance_rad'], START_TOL)
        outside = result['outside_tolerance_rad']
        self.assertEqual(set(outside), set(NEUTRAL))
        self.assertAlmostEqual(outside['waist_roll_joint'], .055)
        self.assertAlmostEqual(outside['waist_pitch_joint'], -.062)
        self.assertAlmostEqual(result['max_abs_error_rad'], .062)
        self.assertCountEqual(result['missing_feedback'], ['waist_yaw_joint', 'left_elbow_joint'])
        self.assertIsNone(preparation.compare_start({})['max_abs_error_rad'])
        json.dumps(result, allow_nan=False)

    def test_start_status_shows_loaded_preview_and_disqualifies_stale_feedback(self):
        transport = RosTransport.__new__(RosTransport)
        transport.lock = threading.RLock()
        transport.mode5_preparation = self.load()
        transport.managed_gateway = SimpleNamespace(process=None, running=False)
        transport.gateway, transport.q = {}, self.start
        transport.state_at = 10.
        transport.graph_ok, transport.graph_error = True, ''
        transport.authority, transport.recovery_chord = None, SimpleNamespace(status={})
        transport.stopped = threading.Event()
        with patch('ros_transport.time.monotonic', return_value=10.05):
            state = transport.snapshot()
        evidence = state['mode5_preparation']
        self.assertEqual(state['phase'], 'DETACHED')
        self.assertEqual(evidence['preflight'], str(self.path.resolve()))
        self.assertEqual(evidence['preview_sha256'], self.report['matches']['locked']['preview_sha256'])
        self.assertTrue(evidence['start_comparison']['matches'])
        with patch('ros_transport.time.monotonic', return_value=10.2):
            stale = transport.snapshot()
        self.assertTrue(stale['connected'])
        self.assertIsNone(stale['mode5_preparation']['start_comparison'])

        opener = Mock()
        opener.open.return_value = nullcontext(io.StringIO(json.dumps(dict(native=state))))
        stdout = io.StringIO()
        with patch('sys.argv', ['robot_request.py', 'status', '--key-file', str(self.path.with_name('no-key'))]), \
                patch('robot_request.build_opener', return_value=opener), redirect_stdout(stdout):
            request_main()
        self.assertEqual(json.loads(stdout.getvalue())['mode5_preparation'], evidence)
        self.assertEqual(opener.open.call_args.args[0].get_method(), 'GET')

    def test_initialize_drift_is_reported_before_arm(self):
        transport = RosTransport.__new__(RosTransport)
        transport.mode5_preparation = self.load()
        transport.lock = threading.RLock()
        transport.state_at = 10.
        transport.q = dict(self.start, waist_roll_joint=.101)
        transport.hand = Mock()
        transport.mode5_profile_ready = Mock(return_value=True)
        transport.verify_configuration, transport.call, transport.arm = Mock(), Mock(), Mock()
        with patch.dict('sys.modules', {'std_srvs.srv': SimpleNamespace(Trigger=Mock())}), \
                patch('ros_transport.time.monotonic', return_value=10.05), \
                self.assertRaisesRegex(RuntimeError, r'no ARM sent; preflight=.*waist_roll_joint=\+0\.05100'):
            transport.initialize(self.release, Mock())
        transport.hand.read.assert_called_once()
        transport.call.assert_not_called()
        transport.arm.assert_not_called()

    def test_unconfirmed_preflight_cannot_enter_profile(self):
        original = copy.deepcopy(self.report)
        for field, invalid in (('stationary_s', .002), ('capture', 'saved_sample'), ('inspect_only', True),
                               ('stationary_window_passed', False), ('age_at_capture_s', .11),
                               ('release_sha256', 'other'), ('model_sha256', 'other'), ('rest_window', None),
                               ('rest_window', dict(original['rest_window'], peak_abs_dq_rad_s=.151))):
            with self.subTest(field=field):
                self.report = dict(original, **{field: invalid})
                with self.assertRaisesRegex(ValueError, 'passing live pull preflight'):
                    self.load()

    def test_tampered_or_unsafe_preview_is_blocked(self):
        with self.preview.open('a') as stream:
            stream.write('\n')
        with self.assertRaisesRegex(ValueError, 'has changed'):
            self.load()
        self.write_preview(duration_ns=150_000_000)
        with self.assertRaisesRegex(ValueError, 'Invalid reviewed quintic'):
            self.load()
        self.write_preview(target=dict(self.target, waist_pitch_joint=.01))
        with self.assertRaisesRegex(ValueError, 'Invalid reviewed quintic'):
            self.load()

    def test_replay_align_uses_gateway_start_limit_only_for_mode5(self):
        transport = RosTransport.__new__(RosTransport)
        transport.stationary = Mock()
        target = {'right_elbow_joint': -.17}
        trajectory = dict(joint_names=list(target), points=[dict(positions=list(target.values()))])
        check = Mock()
        for preparation, tolerance in ((self.load(), .05), (None, .025)):
            with self.subTest(mode5=preparation is not None):
                transport.mode5_preparation = preparation
                transport.stationary.reset_mock()
                transport.align_check(trajectory, check)
                transport.stationary.assert_called_once_with(check, expected=target, tolerance=tolerance)

    def test_endpoint_tolerance_uses_config_limit_only_for_mode5(self):
        transport = RosTransport.__new__(RosTransport)
        transport.stationary = Mock()
        values = dict(endpoint_tolerance_deg=1.4, endpoint_wait_seconds=18.)
        check = Mock()
        for preparation, tolerance_deg in ((self.load(), 2.8), (None, 1.4)):
            with self.subTest(mode5=preparation is not None):
                transport.mode5_preparation = preparation
                transport.stationary.reset_mock()
                transport.settle(self.target, values, check)
                transport.stationary.assert_called_once_with(
                    check, expected=self.target, tolerance=math.radians(tolerance_deg), timeout=18.)
        self.assertEqual(values['endpoint_tolerance_deg'], 1.4)

    def test_waist_hold_check_requires_live_neutral_preparation(self):
        transport = RosTransport.__new__(RosTransport)
        transport.mode5_preparation = self.load()
        transport.lock = threading.RLock()
        transport.state_at = time.monotonic()
        transport.q = dict(self.target, waist_pitch_joint=-.02766)
        transport.gateway_at = transport.state_at
        transport.gateway = dict(mode5_locked_waist='true', mode5_waist_prepared='true', mode_machine='5', mode_pr='0',
                                 mode5_rest_method=REST_METHOD, mode5_rest_ready='true')
        self.assertTrue(transport.mode5_rest_ready())
        transport.require_neutral_waist()
        self.assertEqual(HOLD_TOL, .04)
        transport.q = dict(self.target, waist_roll_joint=.04, waist_pitch_joint=-.04)
        transport.require_neutral_waist()
        for name in NEUTRAL:
            transport.q = dict(self.target, **{name: HOLD_TOL + .001})
            with self.assertRaisesRegex(RuntimeError, 'hold is not confirmed'):
                transport.require_neutral_waist()
        transport.q, transport.state_at = self.target, time.monotonic() - .2
        with self.assertRaisesRegex(RuntimeError, 'hold is not confirmed'):
            transport.require_neutral_waist()
        self.assertFalse(transport.mode5_rest_ready())

    def test_optional_parameters_and_action_scope(self):
        config = json.loads((ROOT / 'config/native.yaml').read_text())
        original = config['g1_arm_sdk_hardware_gateway']['ros__parameters']
        profile = dict(original)
        update_gateway_parameters(profile)
        self.assertEqual(original['controlled_waist_indices'], [12])
        self.assertEqual(profile['controlled_waist_indices'], [12, 13, 14])
        self.assertIs(profile['mode5_locked_waist'], True)
        self.assertIs(profile['service_check_enabled'], True)
        self.assertIn('g1_arm_example', original['required_active_services'])
        self.assertEqual(set(profile['required_active_services']), {'ai_sport', 'basic_service', 'emergency_stop'})
        self.assertEqual(set(profile['required_inactive_services']), {'auto_test_arm', 'auto_test_low', 'g1_arm_example'})
        worker = Worker.__new__(Worker)
        worker.transport = SimpleNamespace(mode5_preparation=self.load())
        worker.release = self.release
        with self.assertRaisesRegex(ValueError, 'only initialization and pull'):
            worker.start(dict(id='blocked', kind='action', action='push', sha256=self.release['sha256']))

    def test_worker_cli_reaches_detached_ipc(self):
        self.path.write_text(json.dumps(self.report))
        token = self.path.with_name('native.token')
        token.write_text('x' * 32)
        argv = ['native_worker.py', '--hardware', '--managed-gateway', '--mode5-preflight', str(self.path),
                '--release', str(ROOT / 'config/selected_bundle.json'), '--token-file', str(token),
                '--journal', str(self.path.with_name('jobs.json')), '--urdf', str(ROOT / 'assets/planner.urdf')]
        ros = Mock()
        transport = Mock(simulation=False)
        transport.snapshot.return_value = dict(phase='DETACHED', controllerRunning=False)
        server = Mock()
        with patch('sys.argv', argv), patch.dict('sys.modules', {'rclpy': ros}), \
                patch('ros_transport.RosTransport', return_value=transport) as create_transport, \
                patch('native_worker.create_server', return_value=server) as create_ipc:
            worker_main()
        preparation = create_transport.call_args.kwargs['mode5_preparation']
        self.assertEqual(preparation.start, self.start)
        worker = create_ipc.call_args.args[0]
        self.assertEqual(worker.state()['native']['phase'], 'DETACHED')
        transport.ensure_gateway.assert_not_called()
        transport.initialize.assert_not_called()
        server.serve_forever.assert_called_once()
        server.server_close.assert_called_once()
        ros.shutdown.assert_called_once()

    def test_supervisor_worker_failure_survives_cleanup(self):
        ros = Mock()
        probe = ros.create_node.return_value
        probe.get_publishers_info_by_topic.return_value = []
        authority = Mock()
        authority.check.return_value = set()
        authority.snapshot.return_value = {'pinned_reader': 'onboard'}
        services = Mock()
        services.service_list.return_value = [dict(name=name, status=status)
            for status, names in ((0, ACTIVE_SERVICES), (1, (*INACTIVE_SERVICES, 'g1_arm_example')))
            for name in names]
        child = Mock()
        child.poll.return_value = 1
        opener = Mock()
        opener.open.side_effect = OSError('worker unavailable')
        args = SimpleNamespace(mode5_preflight=self.path, check_only=False)
        with patch.dict('sys.modules', {'rclpy': ros}), \
                patch('standalone_supervisor.DdsAuthority', return_value=authority), \
                patch('inspect_robot_services.RobotServices', return_value=services), \
                patch('standalone_supervisor.time.monotonic', side_effect=(0., 4., 4., 4.)), \
                patch('standalone_supervisor.signal.signal'), \
                patch('standalone_supervisor.build_opener', return_value=opener), \
                patch('standalone_supervisor.subprocess.Popen', return_value=child) as spawn, \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), \
                self.assertRaisesRegex(RuntimeError, r'worker exited \(code=1\)'):
            run_runtime(args, ROOT, ROOT / 'config/selected_bundle.json', self.release, self.path.parent, True)
        spawn.assert_called_once()
        self.assertTrue(spawn.call_args.kwargs['stdout'].closed)
        child.send_signal.assert_not_called()
        authority.close.assert_called_once()
