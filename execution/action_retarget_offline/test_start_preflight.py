import unittest
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace

import numpy as np

from start_preflight import ALIGN_ACCEL_RAD_S2, ALIGN_SPEED_RAD_S, SLOTS, active_source, alignment, check_feedback
from solver import HERE, NAMES, NS, RUNTIME, SIZE
from mode5_locked import REST_METHOD


def source():
    return dict(host='Unitree', process='basic_service', frames=5000, mode_machine=[5], mode_pr=[0],
                age_s=.012, stationary_s=.3, stationary_ticks=290,
                joints=[dict(slot=i, mode=1, fault=0, q=.1, dq=.005, vol=50, tau_est=.2,
                             max_temperature_c=35) for i in reversed(SLOTS)])


def model():
    return SimpleNamespace(lower=np.full(SIZE, -1.), upper=np.full(SIZE, 1.),
                           velocity=np.full(SIZE, .5), effort=np.full(SIZE, 5.))


class StartPreflightTest(unittest.TestCase):
    def test_shared_native_rest_window_without_dds(self):
        with tempfile.TemporaryDirectory() as folder:
            binary = Path(folder) / 'rest_window_test'
            subprocess.run(['g++', '-std=c++17', '-O2', '-Wall', '-Wextra',
                            '-I', str(RUNTIME / 'native_ws/src/g1_unitree_control/include'),
                            str(HERE / 'test_mode5_rest_window.cpp'), '-o', str(binary)], check=True)
            subprocess.run([str(binary)], check=True)

    def test_selects_one_active_source_with_an_idle_bridge(self):
        good = source()
        snapshot = dict(schema=1, unattributed_frames=0, sources=[dict(frames=0), good])
        self.assertIs(active_source(snapshot), good)
        snapshot['sources'].append(source())
        with self.assertRaisesRegex(ValueError, 'one active'):
            active_source(snapshot)
        snapshot['sources'].pop()
        good['mode_pr'] = [1]
        with self.assertRaisesRegex(ValueError, 'Mode 5 and PR'):
            active_source(snapshot)

    def test_feedback_validates_slots_window_and_joint_status(self):
        good = source()
        q, issues, ready = check_feedback(good, model())
        np.testing.assert_array_equal(q, np.full(SIZE, .1))
        self.assertEqual(issues, {})
        self.assertTrue(ready)
        good['age_s'] = .101
        good['joints'][0].update(slot=14, dq=.031, fault=1)
        good['joints'][1].pop('vol')
        _, issues, ready = check_feedback(good, model())
        self.assertFalse(ready)
        self.assertEqual(issues['waist_pitch_joint'], ['inactive_or_fault', 'moving'])
        self.assertEqual(issues['waist_roll_joint'], ['nonfinite_or_missing_feedback'])
        good['joints'][1]['slot'] = 14
        with self.assertRaisesRegex(ValueError, 'exactly motor slots'):
            check_feedback(good, model())

    def test_alignment_has_exact_rest_endpoints_and_respects_caps(self):
        m = model()
        q0, q1 = np.linspace(-.45, .4, SIZE), np.linspace(.2, -.3, SIZE)
        m.velocity[-1] = .08
        times, curve = alignment(q0, q1, m)
        np.testing.assert_array_equal(curve[[0, -1], 0], [q0, q1])
        np.testing.assert_array_equal(curve[[0, -1], 1:], np.zeros((2, 2, len(NAMES))))
        self.assertLessEqual(np.max(np.abs(curve[:, 1])), ALIGN_SPEED_RAD_S + 1e-9)
        self.assertLessEqual(np.max(np.abs(curve[:, 1, -1])), .08 + 1e-9)
        self.assertLessEqual(np.max(np.abs(curve[:, 2])), ALIGN_ACCEL_RAD_S2 + 1e-9)
        self.assertGreaterEqual(times[-1] / NS, 1.875 * .7 / .08)
        self.assertTrue(np.all(curve[:, 0] >= np.minimum(q0, q1) - 1e-9))
        q1[0] = 1.1
        with self.assertRaisesRegex(ValueError, 'outside joint margins'):
            alignment(q0, q1, m)

    def test_position_window_uses_its_own_peak_and_keeps_feedback_checks(self):
        good = source()
        good.update(stationary_s=.151, stationary_ticks=35, rest_window=dict(
            method=REST_METHOD, passed=True, q_span_rad=.0004, mean_abs_dq_rad_s=.015, peak_abs_dq_rad_s=.073))
        good['joints'][0]['dq'] = .07
        _, issues, ready = check_feedback(good, model())
        self.assertEqual(issues, {})
        self.assertTrue(ready)
        good['rest_window']['q_span_rad'] = .004
        self.assertFalse(check_feedback(good, model())[-1])
        good['rest_window']['q_span_rad'] = .0004
        good['joints'][0]['dq'] = .16
        self.assertIn('moving', check_feedback(good, model())[1]['waist_pitch_joint'])
