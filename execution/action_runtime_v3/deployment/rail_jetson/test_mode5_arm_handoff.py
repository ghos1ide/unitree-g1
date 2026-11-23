import contextlib
import io
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from bundle import ROOT
from mode5_arm_handoff import arm_service, handoff, require_service_released
from standalone_supervisor import main
from test_dds_authority import mode5_discovery


def service_list(arm_status=0):
    active = ('ai_sport', 'basic_service', 'emergency_stop')
    inactive = ('auto_test_arm', 'auto_test_low')
    return ([dict(name=name, status=0, protect=0) for name in active] +
            [dict(name=name, status=1, protect=0) for name in inactive] +
            [dict(name='g1_arm_example', status=arm_status, protect=0)])


class HandoffTest(unittest.TestCase):
    def setUp(self):
        self.state = mode5_discovery()
        self.rows = service_list()
        self.seconds = 10.
        self.services = Mock()
        self.services.service_list.side_effect = lambda: self.rows
        self.services.spin_once.side_effect = lambda *args, **kwargs: self.elapse(.25)
        self.services.node.count_publishers.side_effect = lambda topic: (
            len(self.state['writers']) if topic == '/arm_sdk' else 0)
        clock = NS(monotonic=lambda: self.seconds, sleep=self.elapse)
        self.observer = Mock()
        self.observer.snapshot.return_value = {'discovery': self.state}
        self.observer.checked_state.return_value = self.state
        for name, override in (('service_probe', lambda: contextlib.nullcontext(self.services)),
                               ('time', clock), ('DdsAuthority', Mock(return_value=self.observer))):
            patcher = patch('mode5_arm_handoff.' + name, override)
            patcher.start()
            self.addCleanup(patcher.stop)

    def elapse(self, seconds):
        self.seconds += seconds

    def run_handoff(self, *, enable, drop_writer=False):
        def switch(*, enable, before_send):
            before_send()
            self.rows[-1]['status'] = 0 if enable else 1
            if drop_writer:
                self.state['writers'].clear()

        self.services.switch_arm_example.side_effect = switch
        output, error = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            result = handoff(ROOT, enable=enable)
        self.observer.close.assert_called_once()
        return result, output.getvalue(), error.getvalue()

    def test_service_stop_requires_disappearance_of_writer(self):
        result, _, error = self.run_handoff(enable=False)
        self.assertEqual(result, 2)
        self.assertEqual(self.rows[-1]['status'], 1)
        self.assertIn('stopped, but an /arm_sdk writer remains', error)
        self.services.switch_arm_example.assert_called_once()
        self.observer.check.assert_not_called()

    def test_service_stop_and_clear_dds_allows_handoff(self):
        result, output, _ = self.run_handoff(enable=False, drop_writer=True)
        self.assertEqual(result, 0)
        self.assertIn('Mode 5 handoff clear', output)
        self.assertEqual(self.rows[-1]['status'], 1)
        self.services.switch_arm_example.assert_called_once()
        self.observer.check.assert_called()

    def test_active_commands_block_service_stop(self):
        self.state['messages']['writer'] = 1
        result, _, error = self.run_handoff(enable=False)
        self.assertEqual(result, 2)
        self.assertIn('Arm command traffic was observed', error)
        self.services.switch_arm_example.assert_not_called()

    def test_restore_blocked_by_existing_writer(self):
        self.rows[-1]['status'] = 1
        result, _, error = self.run_handoff(enable=True)
        self.assertEqual(result, 2)
        self.assertIn('Unexpected /arm_sdk writer', error)
        self.services.switch_arm_example.assert_not_called()

    def test_restore_after_empty_graph_enables_service(self):
        self.state['writers'].clear()
        self.rows[-1]['status'] = 1
        result, output, _ = self.run_handoff(enable=True)
        self.assertEqual(result, 0)
        self.assertIn('g1_arm_example active (status=0)', output)
        self.assertEqual(self.rows[-1]['status'], 0)
        self.services.switch_arm_example.assert_called_once()

    def test_missing_emergency_stop_or_protected_service_leaves_state_unchanged(self):
        self.rows[-1]['protect'] = 1
        result, _, error = self.run_handoff(enable=False)
        self.assertEqual(result, 2)
        self.assertIn('is protected', error)
        with self.assertRaisesRegex(RuntimeError, 'Require emergency_stop status=0'):
            arm_service([row for row in self.rows if row['name'] != 'emergency_stop'])
        self.services.switch_arm_example.assert_not_called()

    def test_runtime_requires_service_inactive(self):
        with self.assertRaisesRegex(RuntimeError, '--mode5-arm-service stop'):
            require_service_released(self.rows)
        require_service_released(service_list(arm_status=1))

    @patch('mode5_arm_handoff.handoff', return_value=0)
    @patch('standalone_supervisor.arm_owner_locks', return_value=contextlib.nullcontext())
    @patch('standalone_supervisor.load_release')
    @patch('sys.argv', ['standalone_supervisor.py', '--mode5-arm-service', 'stop', '--confirm-mode5-clear'])
    @patch.dict('os.environ', {'ROS_DOMAIN_ID': '0', 'ROS_LOCALHOST_ONLY': '0'})
    def test_cli_dispatches_explicit_service_change_under_owner_locks(self, load, acquire, switch):
        self.assertEqual(main(), 0)
        acquire.assert_called_once_with(ROOT)
        switch.assert_called_once_with(ROOT, enable=False)
        load.assert_not_called()

    @patch('mode5_arm_handoff.handoff')
    @patch('sys.argv', ['standalone_supervisor.py', '--mode5-arm-service', 'stop'])
    @patch.dict('os.environ', {'ROS_DOMAIN_ID': '0', 'ROS_LOCALHOST_ONLY': '0'})
    def test_cli_requires_confirmation(self, switch):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main()
        switch.assert_not_called()
