"""RobotState requests for service inspection and the explicit arm handoff."""
from contextlib import contextmanager
import json
import sys
import time


ARM_EXAMPLE = 'g1_arm_example'
SERVICE_SWITCH = 1001
SERVICE_LIST = 1003


class RobotServices:
    def __init__(self, node):
        from rclpy import spin_once
        from unitree_api.msg import Request, Response

        self.node, self.spin_once, self.request_type = node, spin_once, Request
        self.identity, self.response = None, None
        node.create_subscription(Response, '/api/robot_state/response', self.receive, 10)
        self.publisher = node.create_publisher(Request, '/api/robot_state/request', 10)

    def receive(self, message):
        identity = message.header.identity
        if self.response is None and (identity.api_id, identity.id) == self.identity:
            self.response = message

    def call(self, api_id, parameter, *, before_send=None):
        request = self.request_type()
        self.identity = (api_id, time.monotonic_ns())
        request.header.identity.api_id, request.header.identity.id = self.identity
        request.parameter = json.dumps(parameter)
        self.response = None
        deadline = time.monotonic() + 5.
        next_query = 0.
        sent = False
        try:
            while self.response is None and time.monotonic() < deadline:
                now = time.monotonic()
                if (api_id == SERVICE_LIST or not sent) and now >= next_query and self.publisher.get_subscription_count():
                    if before_send:
                        before_send()
                    self.publisher.publish(request)
                    sent = True
                    next_query = now + 1.
                self.spin_once(self.node, timeout_sec=.1)
        finally:
            self.identity = None
        if self.response is None:
            if sent and api_id == SERVICE_SWITCH:
                raise RuntimeError('ServiceSwitch outcome unknown; inspect ServiceList before retrying')
            raise RuntimeError('API %d: no response in 5 s' % api_id)
        if self.response.header.status.code != 0:
            raise RuntimeError('API %d returned code %s' % (api_id, self.response.header.status.code))
        return self.response.data

    def service_list(self):
        services = json.loads(self.call(SERVICE_LIST, {}))
        if not isinstance(services, list):
            raise ValueError('Expected a ServiceList array')
        return services

    def switch_arm_example(self, *, enable, before_send):
        self.call(SERVICE_SWITCH, {'name': ARM_EXAMPLE, 'switch': int(enable)}, before_send=before_send)


@contextmanager
def service_probe():
    import rclpy

    rclpy.init()
    node = None
    try:
        node = rclpy.create_node('g1_service_list_probe')
        yield RobotServices(node)
    finally:
        if node is not None:
            node.destroy_node()
        rclpy.shutdown()


def inspect_services():
    try:
        with service_probe() as services:
            print('Robot ServiceList: ' + json.dumps(services.service_list(), sort_keys=True), flush=True)
        return 0
    except (RuntimeError, ValueError) as error:
        print('Robot ServiceList unavailable: ' + str(error), file=sys.stderr, flush=True)
        return 2
