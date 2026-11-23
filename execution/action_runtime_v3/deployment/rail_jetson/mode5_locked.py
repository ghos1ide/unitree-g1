"""Admission for the separately reviewed, locked-waist Mode 5 pull profile."""
from collections import deque
import csv
import hashlib
import json
import math

from inspect_robot_services import ARM_EXAMPLE


ACTION = 'pull'
EXTRA_MOTORS = {'waist_roll_joint': 13, 'waist_pitch_joint': 14}
NEUTRAL = dict.fromkeys(EXTRA_MOTORS, 0.)
MODEL_SHA = 'c0ae739c640c3e2c00d1bdd8810b5d6e59601487bd1a3995859f9543269ee5c8'
START_TOL = .05
START_FEEDBACK_MAX_AGE = .1
HOLD_TOL = .04  # Matches the gateway's Mode 5 neutral admission limit.
MAX_SPEED = .10
MAX_ACCEL = .40
NS = 1_000_000_000
REST_METHOD = 'q_span_and_dq_v2'
ACTIVE_SERVICES = ('ai_sport', 'basic_service', 'emergency_stop')
INACTIVE_SERVICES = ('auto_test_arm', 'auto_test_low')
BINARY = 'native_ws/build/g1_unitree_control/arm_sdk_hardware_gateway'
DATA = 'data/mode5_locked'


def update_gateway_parameters(parameters):
    parameters.update(mode5_locked_waist=True, controlled_waist_indices=[12, 13, 14],
                      service_check_enabled=True, required_active_services=list(ACTIVE_SERVICES),
                      required_inactive_services=[*INACTIVE_SERVICES, ARM_EXAMPLE])


def rest_window_passed(evidence, seconds, samples):
    limits = {'q_span_rad': .003, 'mean_abs_dq_rad_s': .03, 'peak_abs_dq_rad_s': .15}
    return (isinstance(evidence, dict) and evidence.get('method') == REST_METHOD and
            evidence.get('passed') is True and isinstance(seconds, (int, float)) and
            .15 <= seconds <= .18 + 1e-9 and type(samples) is int and 8 <= samples <= 512 and
            all(isinstance(evidence.get(k), (int, float)) and 0. <= evidence[k] <= limit
                for k, limit in limits.items()))


class LockedPreparation:
    def __init__(self, path, release, root):
        self.path = path.resolve()
        model = root.parent / 'action_retarget_offline/assets/g1_29dof_rev_1_0.urdf'
        if hashlib.sha256(model.read_bytes()).hexdigest() != MODEL_SHA:
            raise ValueError('Mode 5 URDF does not match the checked model')
        report = json.loads(self.path.read_text())
        if (report.get('action') != ACTION or report.get('release_sha256') != release['sha256'] or
                report.get('model_sha256') != MODEL_SHA or report.get('capture') != 'live' or
                report.get('inspect_only') is not False or
                report.get('read_only') is not True or report.get('hardware_executable') is not False or
                report.get('hardware_commands_sent') != 0 or report.get('feedback_problems') != {} or
                report.get('stationary_window_passed') is not True or
                not rest_window_passed(report.get('rest_window'), report.get('stationary_s'), report.get('stationary_ticks')) or
                not 0. <= report.get('age_at_capture_s', -1.) <= .1):
            raise ValueError('Require a passing live pull preflight from this Mode 5 model and selected release')
        match = report['matches']['locked']
        if match.get('preview_generated') is not True or match.get('preview_file') != 'locked_alignment_preview.csv':
            raise ValueError('No checked locked-waist alignment preview')
        preview = self.path.parent / match['preview_file']
        self.preview_sha256 = hashlib.sha256(preview.read_bytes()).hexdigest()
        if self.preview_sha256 != match.get('preview_sha256'):
            raise ValueError('Alignment preview has changed; rerun preflight')

        self.action = release['payload']['actions'][ACTION]
        self.target = dict(release['payload']['initialization']['target'], **NEUTRAL)
        initial = self.action['program'][0]
        if (initial.get('kind') != 'join' or initial.get('label') != 'align to pinned task start' or
                not initial['target'] or any(self.target.get(n) != q for n, q in initial['target'].items())):
            raise ValueError('Selected pull start is not the checked preparation target')
        with preview.open(newline='') as source:
            reader = csv.DictReader(source)
            first = next(reader, None)
            trailing = deque(reader, maxlen=1)
        if first is None or not trailing:
            raise ValueError('Alignment preview is missing start or end data')
        last = trailing[0]
        start_ns, duration_ns = int(first['time_ns']), int(last['time_ns'])
        if (start_ns != 0 or duration_ns != match.get('duration_ns') or not 0 < duration_ns <= 180 * NS or
                any(row['kind'] != 'offline_alignment_preview' for row in (first, last))):
            raise ValueError('Invalid alignment duration or event kind')
        self.start = {n: float(first[n + '_rad']) for n in self.target}
        duration = duration_ns / NS
        for name, goal in self.target.items():
            delta = abs(self.start[name] - goal)
            if (not math.isfinite(delta) or not math.isclose(float(last[name + '_rad']), goal, abs_tol=1e-9) or
                    any(float(row[name + s]) != 0. for row in (first, last) for s in ('_rad_s', '_rad_s2')) or
                    1.875 * delta / duration > MAX_SPEED + 1e-9 or
                    (10 / math.sqrt(3)) * delta / duration**2 > MAX_ACCEL + 1e-9):
                raise ValueError('Invalid reviewed quintic for ' + name)
        self.duration_ns = duration_ns

    def compare_start(self, measured):
        missing, outside = [], {}
        peak = None
        for name, reviewed in self.start.items():
            current = measured.get(name)
            if current is None or not math.isfinite(current):
                missing.append(name)
                continue
            error = current - reviewed
            peak = max(peak or 0., abs(error))
            if abs(error) > START_TOL:
                outside[name] = error
        return dict(matches=not (missing or outside), tolerance_rad=START_TOL, max_abs_error_rad=peak,
                    outside_tolerance_rad=outside, missing_feedback=missing)

    def require_start(self, measured, message):
        comparison = self.compare_start(measured)
        if not comparison['matches']:
            outside = sorted(comparison['outside_tolerance_rad'].items(), key=lambda item: abs(item[1]), reverse=True)
            details = [f'{name}={error:+.5f} rad' for name, error in outside]
            if comparison['missing_feedback']:
                details.append('missing feedback: ' + ', '.join(comparison['missing_feedback']))
            raise RuntimeError(f'{message}; preflight={self.path}; tolerance={START_TOL:.3f} rad; '
                               + ', '.join(details))

    def trajectory(self, measured):
        self.require_start(measured, 'Live start drifted from the reviewed preparation preview '
                                     'in ARMED_HOLD; no preparation trajectory sent')
        points = []
        for time_ns, values in ((0, self.start), (self.duration_ns, self.target)):
            sec, nanosec = divmod(time_ns, NS)
            points.append(dict(positions=[values[n] for n in self.target], velocities=[0.] * len(self.target),
                               accelerations=[0.] * len(self.target), time_from_start=dict(sec=sec, nanosec=nanosec)))
        return dict(joint_names=list(self.target), points=points)
