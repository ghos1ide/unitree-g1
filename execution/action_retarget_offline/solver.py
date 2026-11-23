"""Fixed-pelvis pose matching and unloaded inverse-dynamics screening."""

import io
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

import numpy as np
from scipy.interpolate import make_interp_spline
from scipy.optimize import least_squares

HERE = Path(__file__).resolve().parent
RUNTIME = HERE.parent / 'action_runtime_v3'
sys.path.insert(0, str(RUNTIME / 'scripts'))
from workbench_model import OfflineModel

ARM_AXES = ('shoulder_pitch', 'shoulder_roll', 'shoulder_yaw', 'elbow',
            'wrist_roll', 'wrist_pitch', 'wrist_yaw')
WAIST = ('waist_yaw_joint', 'waist_roll_joint', 'waist_pitch_joint')
NAMES = tuple(f'{side}_{axis}_joint' for side in ('left', 'right') for axis in ARM_AXES) + WAIST
SIZE = len(NAMES)
POSE_TOL_M = .001
POSE_TOL_RAD = np.deg2rad(.5)
LIMIT_MARGIN_RAD = .02
VEL_CAP = np.array([.4] * 14 + [.35] * 3)
ACCEL_CAP = 1.5
IK_STEP_NS = 100_000_000
NS = 1_000_000_000


def reference_urdf(path):
    tree = ET.parse(RUNTIME / 'assets/gravity.urdf')
    tree.getroot().set('name', 'g1_offline_reference_not_target_hardware')
    for name in WAIST[1:]:
        joint = tree.find(f"joint[@name='{name}']")
        if joint.get('type') != 'fixed' or joint.find('axis') is None or joint.find('limit') is None:
            raise ValueError(f'Unexpected reference joint: {name}')
        joint.set('type', 'revolute')
    tree.write(path, encoding='utf-8', xml_declaration=True)


class TargetModel(OfflineModel):
    def __init__(self, path):
        super().__init__(path)
        self.path = path
        links = {link.get('name'): link for link in self.robot.findall('link')}
        if links.keys() - self.parents.keys() != {'pelvis'}:
            raise ValueError('Target model must be rooted at pelvis')
        joints = {joint.get('name'): joint for joint in self.robot.findall('joint')}
        if len(joints) != len(self.robot.findall('joint')) or len(self.parents) != len(joints):
            raise ValueError('Duplicate target joint name or child link')
        children = {name: [] for name in links}
        for child, entry in self.joints.items():
            if child not in links or entry[0] not in links:
                raise ValueError('Target joint refers to an unknown link')
            children[entry[0]].append(child)
        visited = set()

        def subtree_mass(name):
            visited.add(name)
            mass = links[name].find('inertial/mass')
            if mass is None:
                weight = 0.
            else:
                weight = float(mass.attrib['value'])
                if not np.isfinite(weight) or weight < 0.:
                    raise ValueError(f'Invalid mass on target link {name}')
            weight += sum(subtree_mass(child) for child in children[name])
            masses[name] = weight
            return weight

        masses = {}
        subtree_mass('pelvis')
        if visited != links.keys():
            raise ValueError('Target model is not a connected tree')
        limits = []
        for name in NAMES:
            joint = joints.get(name)
            if joint is None or joint.get('type') != 'revolute' or joint.find('mimic') is not None:
                raise ValueError(f'Target model needs independent revolute joint {name}')
            child = joint.find('child').get('link')
            axis = self.joints[child][3]
            if not np.isclose(np.linalg.norm(axis), 1., rtol=0., atol=1e-6) or masses[child] <= 0.:
                raise ValueError(f'Target joint requires a unit axis and downstream inertia: {name}')
            limit = joint.find('limit')
            try:
                limits.append([float(limit.get(key)) for key in ('lower', 'upper', 'velocity', 'effort')])
            except (TypeError, AttributeError) as error:
                raise ValueError(f'Missing target joint limit: {name}') from error
        lo, hi, velocity, effort = np.asarray(limits).T
        self.lower, self.upper = lo + LIMIT_MARGIN_RAD, hi - LIMIT_MARGIN_RAD
        self.velocity, self.effort = np.minimum(velocity, VEL_CAP), effort
        if (not np.isfinite(limits).all() or np.any(self.lower >= self.upper)
                or np.any(velocity <= 0) or np.any(effort <= 0)):
            raise ValueError('Invalid target position, velocity or effort limits')

        # Freeze unrecorded joints at nominal angles, including URDF mimic offsets.
        self.nominal = {}

        def nominal(name, chain=()):
            if name in NAMES or name in chain or name not in joints:
                raise ValueError(f'Unsupported mimic chain: {name}')
            if name in self.nominal:
                return self.nominal[name]
            joint = joints[name]
            if joint.get('type') not in ('revolute', 'continuous', 'prismatic'):
                raise ValueError(f'Unsupported nominal joint: {name}')
            mimic = joint.find('mimic')
            value = float(np.clip(0., *self.limits[name])) if name in self.limits else 0.
            if mimic is not None:
                value = (float(mimic.get('multiplier', '1')) * nominal(mimic.get('joint'), (*chain, name))
                         + float(mimic.get('offset', '0')))
            if not np.isfinite(value):
                raise ValueError(f'Invalid nominal joint: {name}')
            self.nominal[name] = value
            return value

        for name, joint in joints.items():
            if name not in NAMES and joint.get('type') != 'fixed':
                nominal(name)


class Dynamics:
    def __init__(self, scratch, target, ros_prefix):
        self.target = target
        self.binary = scratch / 'inverse_dynamics'
        libraries = ros_prefix / 'lib'
        self.env = dict(os.environ, LD_LIBRARY_PATH=os.pathsep.join(
            [str(libraries), *filter(None, [os.environ.get('LD_LIBRARY_PATH')])]))
        subprocess.run(['g++', '-std=c++17', '-O2', '-Wall', '-Wextra',
                        '-I' + str(ros_prefix / 'include'), '-isystem', '/usr/include/eigen3',
                        str(HERE / 'inverse_dynamics.cpp'), '-L' + str(libraries),
                        '-Wl,-rpath,' + str(libraries), '-lkdl_parser', '-lorocos-kdl',
                        '-o', str(self.binary)], check=True, env=self.env)

    def __call__(self, values):
        rows = len(values)
        nominal = self.target.nominal
        n = SIZE + len(nominal)
        full = np.zeros((rows, 3, n))
        full[:, :, :SIZE] = values
        full[:, 0, SIZE:] = list(nominal.values())
        with io.StringIO() as request:
            request.write(f'{n} {rows}\n' + ' '.join((*NAMES, *nominal)) + '\n')
            np.savetxt(request, full.reshape(rows, -1), fmt='%.17g')
            completed = subprocess.run([str(self.binary), str(self.target.path)], input=request.getvalue(),
                                       capture_output=True, text=True, env=self.env)
        if completed.returncode:
            raise ValueError(f'KDL inverse dynamics failed:\n{completed.stderr.strip()}')
        torques = np.loadtxt(io.StringIO(completed.stdout), ndmin=2)
        if torques.shape != (rows, n) or not np.isfinite(torques).all():
            raise ValueError('Invalid inverse dynamics result')
        return torques[:, :SIZE]


def workspace_profile(times, amplitude):
    """A roll/pitch candidate with the same position, speed and acceleration at either stop."""
    rate = np.pi * NS / times[-1]
    angle = rate * (times / NS)
    s, c = np.sin(angle), np.cos(angle)
    profile = np.column_stack((s ** 4, 4 * rate * s ** 3 * c,
                               4 * rate ** 2 * (3 * s ** 2 * c ** 2 - s ** 4)))
    profile[[0, -1]] = 0.
    return profile[:, :, None] * np.asarray(amplitude)


class PoseMatcher:
    def __init__(self, model, frame, moving):
        self.model = model
        self.frame = frame
        self.moving = moving
        self.names = tuple(NAMES[i] for i in moving)
        self.lower, self.upper = model.lower[moving], model.upper[moving]
        self.identity = np.eye(4)

    def correction(self, q, target, previous=None):
        state = dict(self.model.nominal, **dict(zip(NAMES, q)))
        pose = self.model.transform(self.frame, state)
        ref = q[self.moving]
        if (np.max(np.abs(pose - target)) <= 1e-12 and np.all(ref >= self.lower) and np.all(ref <= self.upper)
                and (previous is None or not np.any(previous))):
            return np.zeros_like(ref)
        wanted = target[:3, :]
        center = ref if previous is None else ref + .25 * previous
        weight = .004

        def evaluate(x, jac=False):
            state.update(zip(self.names, x))
            if jac:
                pose, j = self.model.pose_jacobian(self.frame, state, self.names, self.identity)
                # dR/dq = omega x R, evaluated on each column of R.
                d_r = np.cross(j[3:].T[:, None, :], pose[:3, :3].T[None, :, :])
                return np.vstack((4 * j[:3], d_r.transpose(0, 2, 1).reshape(len(x), 9).T,
                                  weight * np.eye(len(x))))
            pose = self.model.transform(self.frame, state)
            error = pose[:3, :] - wanted
            return np.concatenate((4 * error[:, 3], error[:, :3].ravel(), weight * (x - center)))

        result = least_squares(evaluate, np.clip(center, self.lower, self.upper),
                               jac=lambda x: evaluate(x, True), bounds=(self.lower, self.upper),
                               max_nfev=30, ftol=1e-9, xtol=1e-9, gtol=1e-9)
        if not np.isfinite(result.x).all():
            raise ValueError('Non-finite IK result')
        return result.x - ref

    def curve(self, times, base, poses, amplitude, boundaries):
        values = base.copy()
        values[:, :, -2:] += workspace_profile(times, amplitude)
        # Avoid differentiating nearly coincident archived and 100 Hz timestamps.
        steps = max(1, int(round(times[-1] / IK_STEP_NS)))
        knots = np.unique(np.searchsorted(times, np.linspace(0, times[-1], steps + 1)))
        deltas = [boundaries[0]]
        for i in knots[1:-1]:
            deltas.append(self.correction(values[i, 0], poses[i], deltas[-1]))
        deltas.append(boundaries[1])
        zeros = np.zeros(len(self.names))
        spline = make_interp_spline(times[knots] / NS, np.asarray(deltas), k=5,
                                    bc_type=([(1, zeros), (2, zeros)],) * 2)
        correction = np.stack([spline(times / NS, nu=n) for n in range(3)], axis=1)
        correction[[0, -1], 0, :] = boundaries
        correction[[0, -1], 1:, :] = 0.
        values[:, :, self.moving] += correction
        return values


def evaluate(times, values, poses, model, frame, dynamics):
    actual = model.batch_transforms([frame], model.nominal, NAMES, values[:, 0, :])[frame]
    distance = np.linalg.norm(actual[:, :3, 3] - poses[:, :3, 3], axis=1)
    # Frobenius distance gives an accurate small-angle error as well as an SO(3) angle.
    rotation = 2 * np.arcsin(np.clip(np.linalg.norm(actual[:, :3, :3] - poses[:, :3, :3],
                                                   axis=(1, 2)) / np.sqrt(8), 0., 1.))
    torques = dynamics(values)
    peak_vel, peak_accel = np.max(np.abs(values[:, 1:, :]), axis=0)
    effort = np.abs(torques) / model.effort
    clearance = np.minimum(values[:, 0, :] - model.lower, model.upper - values[:, 0, :])
    violations = {
        'position': dict(zip(NAMES, np.maximum(-clearance.min(axis=0), 0.).tolist())),
        'velocity': dict(zip(NAMES, np.maximum(peak_vel - model.velocity, 0.).tolist())),
        'acceleration': dict(zip(NAMES, np.maximum(peak_accel - ACCEL_CAP, 0.).tolist())),
        'effort': dict(zip(NAMES, np.maximum(np.abs(torques).max(axis=0) - model.effort, 0.).tolist())),
    }
    violations = {key: {n: excess for n, excess in joints.items() if excess > 1e-8}
                  for key, joints in violations.items()}
    failed = [key for key, joints in violations.items() if joints]
    if distance.max() > POSE_TOL_M or rotation.max() > POSE_TOL_RAD:
        failed.append('pose')
    cost = np.trapz(np.mean(effort ** 2, axis=1), x=times / times[-1])
    metrics = dict(accepted=not failed, failed=failed,
                   joint_limit_excess={key: violations[key] for key in failed if key in violations},
                   max_error_m=float(distance.max()), max_error_rad=float(rotation.max()),
                   max_speed_rad_s=float(peak_vel.max()), peak_effort_fraction=float(effort.max()),
                   rms_effort_fraction=float(np.sqrt(cost)),
                   min_position_margin_rad=float(clearance.min()), max_accel_rad_s2=float(peak_accel.max()),
                   peak_torque_nm=dict(zip(NAMES, np.max(np.abs(torques), axis=0).tolist())),
                   max_extra_excursion_rad=np.max(np.abs(values[:, 0, -2:] - values[0, 0, -2:]),
                                                  axis=0).tolist())
    return metrics, torques
