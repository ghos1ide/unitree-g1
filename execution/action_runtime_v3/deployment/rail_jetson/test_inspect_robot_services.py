import contextlib
import io
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from inspect_robot_services import RobotServices, inspect_services
from standalone_supervisor import main


class InspectRobotServicesTest(unittest.TestCase):
    def test_only_requests_service_list_and_matches_its_reply(self):
        request = NS(header=NS(identity=NS()), parameter='')
        messages = NS(Request=Mock(return_value=request), Response=object())
        ros = Mock()
        node = ros.create_node.return_value
        pub = node.create_publisher.return_value
        pub.get_subscription_count.return_value = 1

        def reply(*args, **kwargs):
            callback = node.create_subscription.call_args.args[2]
            identity = request.header.identity
            for api_id, request_id, code in ((1001, identity.id, 99), (1003, identity.id + 1, 99),
                                             (1003, identity.id, 0)):
                callback(NS(header=NS(identity=NS(api_id=api_id, id=request_id), status=NS(code=code)),
                            data='[{"name":"ai_sport","status":0}]'))

        ros.spin_once.side_effect = reply
        output = io.StringIO()
        with patch.dict('sys.modules', {'rclpy': ros, 'unitree_api.msg': messages}), contextlib.redirect_stdout(output):
            self.assertEqual(inspect_services(), 0)
        node.create_publisher.assert_called_once_with(messages.Request, '/api/robot_state/request', 10)
        pub.publish.assert_called_once_with(request)
        self.assertEqual((request.header.identity.api_id, request.parameter), (1003, '{}'))
        self.assertIn('"name": "ai_sport", "status": 0', output.getvalue())
        node.destroy_node.assert_called_once()
        ros.shutdown.assert_called_once()

    def test_service_switch_is_scoped_and_never_retries_unknown_result(self):
        request = NS(header=NS(identity=NS()), parameter='')
        messages = NS(Request=Mock(return_value=request), Response=object())
        ros = Mock()
        node = Mock()
        pub = node.create_publisher.return_value
        pub.get_subscription_count.return_value = 1
        clock = NS(monotonic=Mock(side_effect=(10., 10.1, 10.2, 12., 12.1, 15.1)), monotonic_ns=lambda: 42)
        before_send = Mock()
        with patch.dict('sys.modules', {'rclpy': ros, 'unitree_api.msg': messages}), \
                patch('inspect_robot_services.time', clock):
            services = RobotServices(node)
            with self.assertRaisesRegex(RuntimeError, 'ServiceSwitch outcome unknown'):
                services.switch_arm_example(enable=False, before_send=before_send)
        pub.publish.assert_called_once_with(request)
        before_send.assert_called_once_with()
        self.assertEqual(request.header.identity.api_id, 1001)
        self.assertEqual(request.parameter, '{"name": "g1_arm_example", "switch": 0}')

    @patch('standalone_supervisor.load_release')
    @patch('inspect_robot_services.inspect_services', return_value=2)
    @patch('sys.argv', ['standalone_supervisor.py', '--inspect-robot-services'])
    @patch.dict('os.environ', {'ROS_DOMAIN_ID': '0', 'ROS_LOCALHOST_ONLY': '0'})
    def test_inspection_never_loads_or_launches_release(self, inspect, load):
        self.assertEqual(main(), 2)
        inspect.assert_called_once_with()
        load.assert_not_called()
