"""Read controller trajectory progress without blocking the ROS callback thread."""
import threading


class GearProgress:
    def __init__(self, checkpoints, report):
        self.checkpoints = iter(checkpoints)
        self.next = next(self.checkpoints, None)
        self.report = report
        self.lock = threading.Lock()
        self.latest_ns = 0

    def feedback(self, message):
        stamp = message.feedback.desired.time_from_start
        value = stamp.sec * 1000000000 + stamp.nanosec
        with self.lock:
            self.latest_ns = max(self.latest_ns, value)

    def drain(self):
        with self.lock:
            value = self.latest_ns
        while self.next is not None and self.next['time_ns'] <= value:
            self.report(self.next['progress'])
            self.next = next(self.checkpoints, None)
