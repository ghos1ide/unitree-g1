"""Offline progress regressions; never imports ROS or opens hardware."""
import asyncio
from itertools import islice
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from bundle import read, seconds
from cloud_executor import ExecutionAudit, RecordedTaskExecutor, SequenceRuntime
from executor import SequenceExecutor
from gear_progress import GearProgress
from gear_protocol import GearLibrary, concatenate, recipe
from offline_transport import OfflineTransport, offline_join

ROOT = Path(__file__).resolve().parents[2]


def parameters(count, upper=False, final=False):
    return dict(count=count, preAction='EXTEND_DOWNLOCK' if upper else 'NONE',
                cycleMode='FINAL_RETRACT_ONLY' if final else 'FULL_CYCLE')


def feedback(ns):
    return SimpleNamespace(feedback=SimpleNamespace(desired=SimpleNamespace(
        time_from_start=SimpleNamespace(sec=ns // 1000000000, nanosec=ns % 1000000000))))


class FeedbackTests(unittest.TestCase):
    def test_pause_duplicates_late_feedback_and_catchup(self):
        values = []
        clock = GearProgress([dict(time_ns=n * 100, progress=n) for n in (1, 2, 3)], values.append)
        for ns in (0, 99, 99):
            clock.feedback(feedback(ns))
            clock.drain()
        self.assertEqual(values, [])
        clock.feedback(feedback(100))
        self.assertEqual(values, [])  # The ROS callback never executes reporting.
        clock.drain()
        self.assertEqual(values, [1])
        for ns in (100, 50, 100):
            clock.feedback(feedback(ns))
            clock.drain()
        self.assertEqual(values, [1])
        clock.feedback(feedback(350))
        clock.drain()
        clock.drain()
        self.assertEqual(values, [1, 2, 3])

    def test_no_feedback_never_estimates_elapsed_time(self):
        values = []
        clock = GearProgress([dict(time_ns=1, progress=1)], values.append)
        for _ in range(100):
            clock.drain()
        self.assertEqual(values, [])


class GearProgressTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.release = read(ROOT / 'config/selected_bundle.json')
        cls.library = GearLibrary(read(ROOT / 'config/gear_protocol.json'), cls.release)

    def test_all_modes_preserve_motion_goals_and_count_each_cycle(self):
        for count in (1, 3, 9, 17):
            for upper in (False, True):
                for final in (False, True):
                    with self.subTest(count=count, upper=upper, final=final):
                        p = parameters(count, upper, final)
                        steps = list(self.library.program(p))
                        contact = [s for s in steps if s.get('gear_phase') == 'contact']
                        r = recipe(p)
                        remaining = r['repetitions']
                        original = []
                        while remaining:
                            size = min(remaining, 8)
                            remaining -= size
                            keys = [r['repeat']] * size
                            if not remaining and r['tail']:
                                keys.append(r['tail'])
                            original.append(concatenate(self.library.units[k] for k in keys))
                        if not r['repetitions']:
                            original.append(self.library.units[r['tail']])
                        self.assertEqual([s['trajectory'] for s in contact], original)
                        self.assertEqual(sum(s['kind'] == 'hand' for s in steps), 3)
                        self.assertEqual(sum(s.get('gear_phase') == 'exit' for s in steps), 1)

                        # Check business completion timestamps independently of
                        # the implementation's original upper/lower block layout.
                        duration = lambda k: round(seconds(self.library.units[k]['points'][-1]) * 1e9)
                        expected, at_ns = [], duration('ul') if upper else 0
                        for n in range(1, count + 1):
                            at_ns += duration('lu')
                            if not (final and n == count):
                                at_ns += duration('ul')
                            expected.append((at_ns, n))
                        actual, offset = [], 0
                        for step in steps:
                            if step.get('gear_phase') == 'contact':
                                actual.extend((offset + m['time_ns'], m['progress'])
                                              for m in step.get('gear_progress', []))
                                offset += round(seconds(step['trajectory']['points'][-1]) * 1e9)
                            elif step['kind'] == 'milestone':
                                value = step['business_progress']
                                if not actual or actual[-1][1] != value:
                                    actual.append((offset, value))
                        self.assertEqual(actual, expected)

    def test_large_count_stays_lazy_with_original_eight_cycle_block(self):
        for upper in (False, True):
            steps = list(islice(self.library.program(parameters(2147483647, upper)), 6))
            repeat = 'cycle_upper' if upper else 'cycle_lower'
            self.assertEqual(steps[4]['trajectory'], concatenate([self.library.units[repeat]] * 8))
            self.assertEqual(steps[5]['business_progress'], 7 if upper else 8)

    def test_progress_reaches_cloud_inside_one_motion_goal(self):
        for upper in (False, True):
            for final in (False, True):
                with self.subTest(upper=upper, final=final):
                    result, values, contacts = asyncio.run(self.run_cloud(upper, final))
                    self.assertEqual((result.status, result.progress), ('SUCCEEDED', 3))
                    self.assertEqual(values, [1, 2, 3])
                    self.assertEqual(contacts, 1)

    def test_failure_or_cancel_preserves_only_reported_cycles(self):
        for upper in (False, True):
            for failure in ('arm', 'cancel'):
                with self.subTest(upper=upper, failure=failure):
                    result, values, contacts = asyncio.run(self.run_cloud(upper, True, failure))
                    self.assertEqual((result.status, result.progress),
                                     ('CANCELLED' if failure == 'cancel' else 'FAILED', 1))
                    self.assertEqual(values, [1])
                    self.assertEqual(contacts, 1)

    async def run_cloud(self, upper, final, failure=None):
        values = []
        cancel = asyncio.Event()

        class Transport(OfflineTransport):
            contacts = 0

            def arm(self, step, trajectory, check, report_progress=None):
                if step.get('gear_phase') == 'contact':
                    self.contacts += 1
                    if failure == 'arm':
                        report_progress(step['gear_progress'][0]['progress'])
                        return dict(status=6, error_code=-4)
                return super().arm(step, trajectory, check, report_progress)

        async def report(progress, message):
            if progress and (not values or progress != values[-1]):
                values.append(progress)
                if failure == 'cancel':
                    cancel.set()

        transport = Transport(0)
        transport.joints = dict(self.release['payload']['initialization']['target'])
        runtime = SequenceRuntime(SequenceExecutor(transport, offline_join))
        with tempfile.TemporaryDirectory(dir=ROOT / 'data') as folder:
            executor = RecordedTaskExecutor(lambda: self.release, runtime,
                ExecutionAudit(Path(folder) / 'audit.sqlite3'), self.library)
            executor.initialized_sha = self.release['sha256']
            try:
                result = await executor.execute(SimpleNamespace(
                    taskId='offline-gear', taskType='GEAR_OPERATION', total=3,
                    parameters=parameters(3, upper, final)), cancel, report)
            finally:
                await executor.close()
        return result, values, transport.contacts


if __name__ == '__main__':
    unittest.main()
