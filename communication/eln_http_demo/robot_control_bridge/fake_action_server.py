"""Run on PC-B under a sourced ROS2 environment. No physical motion."""
import asyncio
import os
import threading

import rclpy
from rclpy.action import ActionServer, CancelResponse, GoalResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from robot_interfaces.action import RobotTask

from robot_gateway.adapters import LocalExecutor, MockControlAdapter
from robot_gateway.models import SimulationConfig, TaskRequest


def convert(goal):
    parameters = {
        "ROBOT_INITIALIZE": {},
        "NWS_CYCLE": dict(count=goal.count),
        "NLG_WOW_OPERATION": dict(wowSignal=goal.wow_signal),
        "GEAR_OPERATION": dict(count=goal.count, preAction=goal.pre_action, cycleMode=goal.cycle_mode),
    }.get(goal.task_type)
    return TaskRequest(taskId=goal.task_id, taskType=goal.task_type,
                       callbackUrl="http://localhost/unused", parameters=parameters)


class FakeActionServer(Node):
    def __init__(self):
        super().__init__("fake_robot_control")
        self.lock = threading.Lock()
        self.busy = False
        self.initialized = False
        self.seen = set()
        self.executor_adapter = LocalExecutor(MockControlAdapter(float(os.getenv("MOCK_STEP_SECONDS", "0.5"))))
        self.action_server = ActionServer(self, RobotTask, os.getenv("ROS_ACTION_NAME", "/robot_task"),
            execute_callback=self.execute, goal_callback=self.goal, cancel_callback=self.cancel,
            callback_group=ReentrantCallbackGroup())

    def goal(self, goal):
        try:
            request = convert(goal)
            SimulationConfig(fault=goal.simulation_fault or "NONE", occurrence=goal.simulation_occurrence or 1)
        except ValueError:
            return GoalResponse.REJECT
        with self.lock:
            if self.busy or goal.task_id in self.seen:
                return GoalResponse.REJECT
            if request.taskType != "ROBOT_INITIALIZE" and not self.initialized:
                return GoalResponse.REJECT
            self.busy = True
            self.seen.add(goal.task_id)
            if request.taskType == "ROBOT_INITIALIZE":
                self.initialized = False
        self.get_logger().info(f"ACCEPTED {goal.task_id} {goal.task_type} {request.parameters}")
        return GoalResponse.ACCEPT

    def cancel(self, handle):
        self.get_logger().info(f"CANCEL_REQUESTED {handle.request.task_id}")
        return CancelResponse.ACCEPT

    def execute(self, handle):
        progress = 0
        result = RobotTask.Result()

        class Cancellation:
            def is_set(self):
                return handle.is_cancel_requested

        async def report(completed, message):
            nonlocal progress
            progress = completed
            feedback = RobotTask.Feedback()
            feedback.status, feedback.progress, feedback.message = "RUNNING", completed, message
            handle.publish_feedback(feedback)
            self.get_logger().info(f"RUNNING {handle.request.task_id} progress={completed} {message}")

        try:
            request = convert(handle.request)
            self.executor_adapter.configure_simulation(SimulationConfig(
                fault=handle.request.simulation_fault or "NONE",
                occurrence=handle.request.simulation_occurrence or 1))
            outcome = asyncio.run(self.executor_adapter.execute(request, Cancellation(), report))
            result.success = outcome.status == "SUCCEEDED"
            result.status, result.progress = outcome.status, outcome.progress
            result.message, result.error_code = outcome.message, outcome.error_code or ""
            self.get_logger().info(f"{outcome.status} {request.taskId} progress={outcome.progress} {outcome.message}")
            if outcome.status == "CANCELLED":
                handle.canceled()
            elif outcome.status == "SUCCEEDED":
                handle.succeed()
            else:
                handle.abort()
            with self.lock:
                if request.taskType == "ROBOT_INITIALIZE":
                    self.initialized = result.success
                elif outcome.status == "FAILED":
                    self.initialized = False
        except Exception as exc:
            self.get_logger().error(f"Control execution failed: {exc}")
            self.initialized = False
            result.success, result.status, result.progress = False, "FAILED", progress
            result.error_code, result.message = "2004", "Control adapter failed"
            handle.abort()
        finally:
            with self.lock:
                self.busy = False
        return result


def main():
    rclpy.init()
    node = FakeActionServer()
    executor = MultiThreadedExecutor(num_threads=4)
    executor.add_node(node)
    try:
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        executor.shutdown()
        node.action_server.destroy()
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
