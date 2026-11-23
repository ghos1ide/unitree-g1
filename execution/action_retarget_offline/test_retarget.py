import csv
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
import xml.etree.ElementTree as ET

import numpy as np

from retarget import continuity, export_csv, stages
from solver import (NAMES, NS, RUNTIME, SIZE, Dynamics, OfflineModel, PoseMatcher,
                    TargetModel, evaluate, reference_urdf, workspace_profile)


class RetargetTests(unittest.TestCase):
    def test_locked_urdf_is_not_a_target(self):
        with self.assertRaisesRegex(ValueError, 'waist_roll_joint'):
            TargetModel(RUNTIME / 'assets/gravity.urdf')

    def test_locked_mapping_keeps_source_rows_and_holds(self):
        names = list(reversed(NAMES[7:15]))
        trajectory = {'joint_names': names}
        from retarget import digest
        program = [dict(kind='recorded', label='move', side='right', trajectory_sha256=digest(trajectory)),
                   dict(kind='hand', label='grip', side='right', duration=1, target_q=[.4] * 6)]
        action = dict(plan=dict(arm='right', segments=[{'trajectory': trajectory}, {'trajectory': {}}]),
                      program=program)
        positions = np.array([list(np.arange(8) / 10), list(np.arange(8) / 11)])
        data = np.hstack([positions, np.zeros((2, 16))])

        def sampler(binary, request):
            self.assertEqual(request, trajectory)
            return np.array([0, NS]), data, {}

        from unittest.mock import patch
        with patch('retarget.sample_arm', sampler):
            (move, times, values, _), (hand, hand_times, holds, _) = list(stages(
                action, dict.fromkeys(NAMES[:-2], .25), None, [.012, -.034]))
        np.testing.assert_array_equal(values[:, :, [NAMES.index(n) for n in names]],
                                      data.reshape(2, 3, 8))
        np.testing.assert_array_equal(values[:, 0, :7], np.full((2, 7), .25))
        np.testing.assert_array_equal(values[:, 0, -2:], [[.012, -.034]] * 2)
        np.testing.assert_array_equal(holds[:, 0], np.tile(values[-1, 0], (len(holds), 1)))
        self.assertFalse(np.any(holds[:, 1:]))
        self.assertEqual(hand['start_ns'], move['end_ns'])
        self.assertEqual(hand['hand']['target_q'], [.4] * 6)
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / 'locked.csv'
            export_csv(path, [(move, times, values, np.zeros((2, SIZE))),
                              (hand, hand_times, holds, np.zeros((len(holds), SIZE)))])
            with path.open() as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[1]['time_ns'], rows[2]['time_ns'])
            self.assertEqual(float(rows[0]['waist_roll_joint_rad']), .012)
            self.assertEqual(float(rows[0]['waist_roll_joint_rad_s']), 0.)

    def test_pose_match_removes_tool_shift_with_nonzero_waist(self):
        frame = 'right_wrist_yaw_link'
        q = np.array([1., -.06, .56, -.18, 1.4, -.53, .78,
                      1., .06, -.56, -.18, -1.4, -.53, -.78, .1, 0., 0.])
        times = np.linspace(0, 4 * NS, 401, dtype=np.int64)
        base = np.zeros((len(times), 3, SIZE))
        base[:, 0, :] = q
        old = OfflineModel(RUNTIME / 'assets/planner.urdf')
        poses = old.batch_transforms([frame], {}, NAMES, base[:, 0, :])[frame]
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / 'shifted.urdf'
            reference_urdf(path)
            tree = ET.parse(path)
            tree.find("joint[@name='right_wrist_yaw_joint']/origin").set('xyz', '.061 0 0')
            tree.write(path)
            target = TargetModel(path)
            matcher = PoseMatcher(target, frame, list(range(7, 15)))
            correction = matcher.correction(q, poses[0])
            curve = matcher.curve(times, base, poses, (.05, 0.), (correction, correction))
            dummy = lambda states: np.zeros((len(states), SIZE))
            locked, _ = evaluate(times, base, poses, target, frame, dummy)
            adapted, _ = evaluate(times, curve, poses, target, frame, dummy)
            np.testing.assert_array_equal(curve[:, :, :7], base[:, :, :7])
            np.testing.assert_array_equal(curve[[0, -1], 1:, :], base[[0, -1], 1:, :])
            continuity(curve[-1], curve[0])
            self.assertAlmostEqual(locked['max_error_m'], .015, places=10)
            self.assertLess(adapted['max_error_m'], .0001)
            self.assertEqual(adapted['max_extra_excursion_rad'], [.05, 0.])
            self.assertTrue(adapted['accepted'], adapted)
            refused, _ = evaluate(times, curve, poses, target, frame,
                                  lambda states: dummy(states) + 1.1 * target.effort)
            self.assertFalse(refused['accepted'])
            self.assertIn('waist_pitch_joint', refused['joint_limit_excess']['effort'])

    def test_profile_and_boundary_guards(self):
        times = np.linspace(0, NS, 1001, dtype=np.int64)
        values = workspace_profile(times, (.05, -.02))
        np.testing.assert_array_equal(values[[0, -1]], 0.)
        np.testing.assert_allclose(values[500, 0], [.05, -.02])
        np.testing.assert_allclose(np.gradient(values[:, 0], .001, axis=0)[1:-1],
                                   values[1:-1, 1], atol=3e-6)
        before, after = np.zeros((3, SIZE)), np.zeros((3, SIZE))
        after[2, -1] += .1
        with self.assertRaisesRegex(ValueError, 'Discontinuous'):
            continuity(before, after)

    def test_kdl_against_a_single_joint_with_known_gravity_and_inertia(self):
        prefix = Path('/opt/ros/foxy')
        if not (prefix / 'include/kdl/treeidsolver_recursive_newton_euler.hpp').is_file():
            self.skipTest('ROS Foxy KDL development libraries unavailable')
        pendulum = '''<robot name="test"><link name="pelvis"/>
          <link name="arm"><inertial><origin xyz="0.2 0 0"/><mass value="2"/>
            <inertia ixx="0.03" iyy="0.03" izz="0.03" ixy="0" ixz="0" iyz="0"/>
          </inertial></link><joint name="pitch" type="revolute"><parent link="pelvis"/>
            <child link="arm"/><axis xyz="0 1 0"/>
            <limit lower="-2" upper="2" velocity="1" effort="10"/></joint></robot>'''
        with TemporaryDirectory() as tmp:
            scratch = Path(tmp)
            dynamics = Dynamics(scratch, None, prefix)
            model = scratch / 'test.urdf'
            model.write_text(pendulum)
            result = subprocess.run([str(dynamics.binary), str(model)], env=dynamics.env,
                                    input='1 3\npitch\n0 0 0\n0 0 1\n1.5707963267948966 0 0\n',
                                    capture_output=True, text=True, check=True)
        gravity = -2 * 9.81 * .2
        np.testing.assert_allclose([float(x) for x in result.stdout.split()],
                                   [gravity, gravity + .03 + 2 * .2 ** 2, 0.], atol=1e-12)


if __name__ == '__main__':
    unittest.main()
