import contextlib
import io
import unittest
from unittest.mock import Mock, patch

from ros_transport import jetson_hand
from standalone_supervisor import SDK_ROOT, inspect_right_hand, main


class RightHandProbeTest(unittest.TestCase):
    def probe(self, hand, *, readers=1, writers=1):
        ros = Mock()
        node = ros.create_node.return_value
        node.count_subscribers.return_value = readers
        node.count_publishers.side_effect = {'/api/rh56_hand/response': writers, '/inspire/cmd': 0}.__getitem__
        out, err = io.StringIO(), io.StringIO()
        with patch.dict('sys.modules', {'rclpy': ros}), patch('ros_transport.jetson_hand', return_value=hand), \
                patch('standalone_supervisor.time.monotonic', side_effect=(0., 3.)), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = inspect_right_hand()
        node.count_subscribers.assert_called_once_with('/api/rh56_hand/request')
        node.destroy_node.assert_called_once()
        ros.shutdown.assert_called_once()
        return result, out.getvalue(), err.getvalue()

    def test_probe_reads_and_checks_only_right_hand(self):
        reply = dict(code=0, hand='right', online=True, timestamp_ms=123,
                     feedback_q=[.5] * 6, target_q=[.5] * 6, error=[0] * 6, temperature=[30] * 6)
        hand = jetson_hand('eth0', SDK_ROOT)
        with patch.object(hand, '_exchange', return_value=reply) as rpc:
            code, out, _ = self.probe(hand)
        rpc.assert_called_once()
        self.assertRegex(rpc.call_args.args[0], r'^get right [0-9]+$')
        self.assertEqual(code, 0)
        self.assertIn('Right hand feedback ready:', out)

    def test_timeout_reports_discovery_and_closes_client(self):
        hand = Mock()
        hand.read.side_effect = TimeoutError('DDS reply timed out (3104)')
        code, out, err = self.probe(hand, readers=0, writers=0)
        self.assertEqual(code, 2)
        self.assertIn('"response_writers": 0', out)
        self.assertIn('"request_readers": 0', out)
        self.assertIn('3104', err)
        hand.read.assert_called_once_with('right')
        hand.close.assert_called_once()

    def test_duplicate_rpc_writers_block_probe(self):
        hand = Mock()
        code, _, err = self.probe(hand, writers=2)
        self.assertEqual(code, 2)
        self.assertIn('Multiple RH56 RPC response writers', err)
        hand.read.assert_not_called()

    @patch('standalone_supervisor.load_release')
    @patch('standalone_supervisor.inspect_right_hand', return_value=0)
    @patch('sys.argv', ['standalone_supervisor.py', '--inspect-right-hand'])
    @patch.dict('os.environ', {'ROS_DOMAIN_ID': '0', 'ROS_LOCALHOST_ONLY': '0'})
    def test_cli_inspection_does_not_load_or_start_runtime(self, inspect, load):
        self.assertEqual(main(), 0)
        inspect.assert_called_once_with()
        load.assert_not_called()
