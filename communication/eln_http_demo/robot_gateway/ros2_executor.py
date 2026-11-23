import asyncio
import threading
import time
from functools import partial

from .adapters import Result
from .models import SimulationConfig


class Ros2Executor:
    """ROS spins on its own thread; only the asyncio thread updates task state."""

    def __init__(self, settings):
        import rclpy
        from rclpy.action import ActionClient
        from rclpy.context import Context
        from rclpy.executors import MultiThreadedExecutor
        from robot_interfaces.action import RobotTask

        self.settings, self.action_type = settings, RobotTask
        self.simulation_config = SimulationConfig()
        self.context = Context()
        rclpy.init(context=self.context)
        self.node = rclpy.create_node("eln_task_gateway", context=self.context)
        self.client = ActionClient(self.node, RobotTask, settings.action_name)
        self.spinner = MultiThreadedExecutor(num_threads=2, context=self.context)
        self.spinner.add_node(self.node)
        self.thread = threading.Thread(target=self.spinner.spin, daemon=True)
        self.thread.start()

    def configure_simulation(self, config):
        self.simulation_config = config

    async def await_future(self, future, timeout):
        deadline = time.monotonic() + timeout
        while not future.done():
            if time.monotonic() >= deadline:
                raise TimeoutError("ROS2 action response timed out")
            await asyncio.sleep(0.02)
        return future.result()

    async def execute(self, request, cancel, report):
        from action_msgs.msg import GoalStatus

        available = await asyncio.get_running_loop().run_in_executor(
            None, partial(self.client.wait_for_server, timeout_sec=3.0))
        if not available:
            return Result("FAILED", 0, "ROS2 action server unavailable", "2004")
        goal = self.action_type.Goal()
        goal.task_id, goal.task_type = request.taskId, request.taskType
        goal.count = request.parameters.get("count", 0)
        goal.wow_signal = request.parameters.get("wowSignal", "")
        goal.pre_action = request.parameters.get("preAction", "")
        goal.cycle_mode = request.parameters.get("cycleMode", "")
        goal.simulation_fault = self.simulation_config.fault
        goal.simulation_occurrence = self.simulation_config.occurrence
        feedback = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def receive(message):
            loop.call_soon_threadsafe(feedback.put_nowait, message.feedback)

        goal_future = self.client.send_goal_async(goal, feedback_callback=receive)
        try:
            handle = await self.await_future(goal_future, 5.0)
        except TimeoutError:
            # If acknowledgement arrives late, request stop instead of leaving an orphan goal.
            def cancel_late(future):
                late = future.result()
                if late and late.accepted:
                    late.cancel_goal_async()
            goal_future.add_done_callback(cancel_late)
            raise
        if not handle.accepted:
            return Result("FAILED", 0, "Controller rejected goal (busy, uninitialized or unsafe)", "2004")
        result_future = handle.get_result_async()
        deadline = time.monotonic() + self.settings.action_timeout
        cancel_sent = False
        try:
            while not result_future.done() or not feedback.empty():
                while not feedback.empty():
                    item = feedback.get_nowait()
                    if item.status != "RUNNING":
                        raise ValueError("invalid ROS2 feedback status")
                    await report(item.progress, item.message)
                if cancel.is_set() and not cancel_sent:
                    handle.cancel_goal_async()
                    cancel_sent = True
                if time.monotonic() >= deadline:
                    raise TimeoutError("Controller result unknown after action timeout")
                await asyncio.sleep(0.02)
            envelope = result_future.result()
            result = envelope.result
            expected = {
                GoalStatus.STATUS_SUCCEEDED: "SUCCEEDED",
                GoalStatus.STATUS_ABORTED: "FAILED",
                GoalStatus.STATUS_CANCELED: "CANCELLED",
            }.get(envelope.status)
            if result.status != expected or result.success != (expected == "SUCCEEDED"):
                raise ValueError("inconsistent ROS2 action result")
            return Result(result.status, result.progress, result.message, result.error_code or None)
        except Exception:
            handle.cancel_goal_async()
            raise

    async def close(self):
        await asyncio.get_running_loop().run_in_executor(
            None, partial(self.spinner.shutdown, timeout_sec=5.0))
        self.thread.join(timeout=5)
        self.client.destroy()
        self.node.destroy_node()
        self.context.shutdown()
