import contextlib
import io
import json
import time
import unittest
from unittest.mock import patch

from bundle import ROOT
from dds_authority import DdsAuthority, LOWCMD, arm_sdk_evidence, native_endpoints, validate
from standalone_supervisor import inspect_arm_sdk, main


def discovery(process='g1_arm_example', frames=0):
    return dict(observed_seconds=4.2, unknown_messages=0,
                participants={'body': dict(host='Unitree', process=process, pid='42')},
                writers=[dict(guid='writer', participant='body', type=LOWCMD)],
                readers=[dict(guid='reader', participant='body', type=LOWCMD),
                         dict(guid='observer', participant='local', type=LOWCMD)],
                messages={'writer': frames} if frames else {})


def mode5_discovery():
    state = discovery(process='python3')
    state['participants']['motion'] = dict(host='Unitree', process='humanoid', pid='3410')
    state['readers'][0]['participant'] = 'motion'
    state['participants']['body']['pid'] = '2890'
    return state


class DdsAuthorityTest(unittest.TestCase):
    def test_evidence_resolves_participant_pairs_and_retains_frame_counts(self):
        state = discovery(frames=7)
        state['messages']['gone'] = 2
        state['unknown_messages'] = 1
        result = arm_sdk_evidence(state)
        writer = result['writers'][0]
        self.assertEqual((writer['host'], writer['process'], writer['pid']), ('Unitree', 'g1_arm_example', '42'))
        self.assertEqual(writer['matching_readers'], ['reader'])
        self.assertEqual(writer['frames'], 7)
        self.assertEqual(result['messages']['gone'], 2)
        self.assertEqual(result['unknown_messages'], 1)
        self.assertIsNone(result['readers'][1]['process'])

    def test_unidentified_or_active_writer_still_blocks(self):
        with self.assertRaisesRegex(RuntimeError, 'identified onboard'):
            native_endpoints(discovery(process='unknown'))
        missing_reader = discovery()
        missing_reader['readers'].pop(0)
        with self.assertRaisesRegex(RuntimeError, 'identified onboard'):
            native_endpoints(missing_reader)
        with self.assertRaisesRegex(RuntimeError, 'sent arm commands'):
            validate(discovery(frames=1), {'writer'})
        validate(discovery(), {'writer'})

    def mode5_authority(self, state):
        with patch('dds_authority.subprocess.Popen') as launch, patch('dds_authority.threading.Thread'):
            launch.return_value.poll.return_value = None
            observer = DdsAuthority(ROOT, mode5=True)
        self.addCleanup(observer.close)
        observer.state, observer.at = state, time.monotonic()
        return observer

    def test_mode5_python_writer_blocks_even_without_frames(self):
        state = mode5_discovery()
        observer = self.mode5_authority(state)
        with self.assertRaisesRegex(RuntimeError, r'Unitree/python3 pid=2890 guid=writer'):
            observer.check()
        self.assertIsNone(observer.snapshot()['pinned_reader'])
        state['writers'].clear()
        self.assertEqual(observer.check(), set())
        self.assertEqual(observer.snapshot()['pinned_reader'], 'reader')

    def test_mode5_reader_must_be_unique_and_keep_its_identity(self):
        state = mode5_discovery()
        state['writers'].clear()
        observer = self.mode5_authority(state)
        reader = state['readers'].pop(0)
        with self.assertRaisesRegex(RuntimeError, 'exactly one onboard Unitree/humanoid'):
            observer.check()
        state['readers'].extend((reader, dict(reader, guid='other-reader')))
        with self.assertRaisesRegex(RuntimeError, 'exactly one onboard Unitree/humanoid'):
            observer.check()
        state['readers'].pop()
        self.assertEqual(observer.check(), set())
        reader['guid'] = 'restarted'
        with self.assertRaisesRegex(RuntimeError, 'reader changed'):
            observer.check()
        self.assertEqual(observer.snapshot()['pinned_reader'], 'reader')

    def test_mode5_only_our_gateway_can_publish_after_takeover(self):
        state = mode5_discovery()
        foreign = state['writers'].pop()
        observer = self.mode5_authority(state)
        self.assertEqual(observer.check(), set())
        own = dict(guid='gateway', participant='local', type=LOWCMD)
        state['writers'].append(own)
        state['messages']['gateway'] = 10
        self.assertEqual(observer.check({'gateway'}), set())
        state['writers'].append(foreign)
        with self.assertRaisesRegex(RuntimeError, 'Another /arm_sdk writer is present'):
            observer.check({'gateway'})
        state['writers'].clear()
        self.assertEqual(observer.check(), set())
        state['messages']['writer'] = 1
        with self.assertRaisesRegex(RuntimeError, 'sent commands during discovery: writer'):
            observer.check()

    def test_mode5_unattributed_messages_block_without_a_writer(self):
        state = mode5_discovery()
        state['writers'].clear()
        state['unknown_messages'] = 1
        observer = self.mode5_authority(state)
        with self.assertRaisesRegex(RuntimeError, 'unknown publisher sent arm commands'):
            observer.check()

    @patch('standalone_supervisor.DdsAuthority')
    def test_inspect_reports_identity_error_and_closes_observer(self, observer_cls):
        observer = observer_cls.return_value
        observer.process.poll.return_value = None
        observer.snapshot.return_value = {'discovery': discovery(process='unknown')}
        observer.check.side_effect = RuntimeError('identity mismatch')
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            self.assertEqual(inspect_arm_sdk(ROOT), 2)
        evidence = json.loads(output.getvalue().split(': ', 1)[1])
        self.assertEqual(evidence['writers'][0]['process'], 'unknown')
        self.assertIn('DDS ownership blocked: identity mismatch', errors.getvalue())
        observer.close.assert_called_once()

    @patch('standalone_supervisor.load_release')
    @patch('standalone_supervisor.inspect_arm_sdk', return_value=0)
    @patch('sys.argv', ['standalone_supervisor.py', '--inspect-arm-sdk'])
    @patch.dict('os.environ', {'ROS_DOMAIN_ID': '0', 'ROS_LOCALHOST_ONLY': '0'})
    def test_inspect_dispatches_before_loading_release(self, inspect, load):
        self.assertEqual(main(), 0)
        inspect.assert_called_once_with(ROOT, mode5=False)
        load.assert_not_called()

    @patch('standalone_supervisor.load_release')
    @patch('standalone_supervisor.inspect_arm_sdk', return_value=0)
    @patch('sys.argv', ['standalone_supervisor.py', '--inspect-mode5-arm-sdk'])
    @patch.dict('os.environ', {'ROS_DOMAIN_ID': '0', 'ROS_LOCALHOST_ONLY': '0'})
    def test_mode5_inspect_dispatches_before_loading_release(self, inspect, load):
        self.assertEqual(main(), 0)
        inspect.assert_called_once_with(ROOT, mode5=True)
        load.assert_not_called()
