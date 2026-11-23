"""Headless recorded-action sequence; transports own real feedback and authority.

The CLI only materializes offline trajectories. It never creates a ROS node or
hardware client. A production transport must independently enforce ownership.
"""
import argparse
import copy
import json
from pathlib import Path
import sys
import threading

from bundle import ROOT, digest, read, write
from releases import load_release


class Cancelled(RuntimeError):
    pass


class LocalJoinPlanner:
    def __init__(self, urdf):
        sys.path.insert(0, str(ROOT / 'scripts'))
        from workbench_model import OfflineModel
        self.model = OfflineModel(urdf)

    def __call__(self, measured, target, parameters, velocity=.7, acceleration=.7):
        from rosidl_runtime_py.convert import message_to_ordereddict
        from task_path_smoothing import joint_line_trajectory
        from task_planning_config import parameters_from_values
        from task_trajectory_speed import scale_arm_segments, motion_limits
        from task_trajectory import samples
        names = list(target)
        trajectory = joint_line_trajectory(names, [measured[n] for n in names], [target[n] for n in names])
        params = parameters_from_values(parameters)
        segments, _ = scale_arm_segments([dict(label='local join', trajectory=trajectory)], 1., self.model,
            params, policy='uniform', velocity=velocity, acceleration=acceleration)
        trajectory = segments[0]['trajectory']
        vmax, amax = motion_limits(trajectory, self.model, velocity, acceleration, params)
        for positions in samples(trajectory, vmax, amax):
            for name, value in zip(names, positions):
                lo, hi = self.model.limits[name]
                if not lo + .02 - 1e-7 <= value <= hi - .02 + 1e-7:
                    raise ValueError('Local join exceeds joint margin: ' + name)
        return message_to_ordereddict(trajectory)


def scales(action):
    record = action['plan'].get('speed_scaling', {})
    return record.get('velocity_limit_scaling', .7), record.get('acceleration_limit_scaling', .7)


def materialize(envelope, name, measured, planner):
    """Offline reference, not a runnable startup plan from a stale measured state."""
    action = envelope['payload']['actions'][name]
    parameters = action['plan']['planning_config']['values']
    q = dict(measured)
    result = []
    for original in action['program']:
        step = copy.deepcopy(original)
        if step['kind'] == 'join':
            step['trajectory'] = planner(q, step['target'], parameters, *scales(action))
        if step['kind'] in ('join', 'recorded'):
            trajectory = step['trajectory']
            names = trajectory['joint_names']
            if step['kind'] == 'recorded':
                if any(abs(q[n] - v) > .025 for n, v in zip(names, trajectory['points'][0]['positions'])):
                    raise ValueError('Program lacks an alignment to recorded start')
                if digest(trajectory) != original['trajectory_sha256']:
                    raise ValueError('Recorded curve changed during materialization')
            q.update(zip(names, trajectory['points'][-1]['positions']))
        result.append(step)
    return dict(release_sha256=envelope['sha256'], action=name, steps=result,
                recorded_arrays_unchanged=True, hardware_commands_sent=0, hardware_executable=False,
                collision_checked=False, final_joints=q)


class SequenceExecutor:
    """No automatic ARM, output acquisition, uncertain-goal retry, or fault reset.

    The transport must check its authority grant before every command, and stop
    both arms and active fingers on failure. Cancellation uses a separate path.
    """
    def __init__(self, transport, planner):
        self.transport = transport
        self.planner = planner
        self.cancelled = threading.Event()
        self.lock = threading.Lock()

    def check_cancelled(self):
        if self.cancelled.is_set():
            raise Cancelled('Task cancelled; interrupted contact segments are never resumed')

    def check(self):
        self.check_cancelled()
        self.transport.assert_authority()

    def cancel(self):
        self.cancelled.set()
        return self.transport.stop_and_confirm()

    def execute(self, envelope, name, on_event=lambda event: None):
        if not self.lock.acquire(blocking=False):
            raise RuntimeError('Executor occupied')
        evidence = []
        try:
            # A stopped executor is not reset implicitly by submitting another task.
            self.check()
            action = envelope['payload']['actions'][name]
            parameters = action['plan']['planning_config']['values']
            self.transport.verify_configuration(action)
            self.transport.begin(action, self.check)
            for index, step in enumerate(action['program']):
                self.check()
                on_event(dict(status='RUNNING', stage=index + 1, label=step['label'],
                              release_sha256=envelope['sha256']))
                measured = self.transport.stationary(self.check)
                if step['kind'] == 'hand':
                    self.transport.set_stage(step['side'], 'normal', parameters, self.check)
                    result = self.transport.hand_transition(step, self.check)
                else:
                    trajectory = (self.planner(measured, step['target'], parameters, *scales(action))
                                  if step['kind'] == 'join' else copy.deepcopy(step['trajectory']))
                    if step['kind'] == 'recorded' and digest(trajectory) != step['trajectory_sha256']:
                        raise RuntimeError('Immutable recording changed')
                    self.transport.set_stage(step['side'], step['profile'], parameters, self.check)
                    # Fresh measured admission remains necessary after a local solve or gain switch.
                    self.transport.align_check(trajectory, self.check)
                    self.check()
                    result = self.transport.arm(step, trajectory, self.check)
                    if result.get('status') != 4 or result.get('error_code') != 0:
                        raise RuntimeError('Arm action did not complete successfully')
                    endpoint = dict(zip(trajectory['joint_names'], trajectory['points'][-1]['positions']))
                    self.transport.settle(endpoint, parameters, self.check)
                evidence.append(dict(label=step['label'], result=result))
            self.transport.set_stage(action['plan']['arm'], 'normal', parameters, self.check)
            self.check()
            return dict(status='SUCCEEDED', evidence=evidence, release_sha256=envelope['sha256'],
                        external_signal_verified=False)
        except Exception as error:
            self.cancelled.set()
            try:
                stopped = self.transport.stop_and_confirm()
            except Exception:
                stopped = False
            return dict(status='CANCELLED' if isinstance(error, Cancelled) and stopped else
                        'FAILED' if stopped else 'STOP_UNCONFIRMED',
                        message=str(error), stop_confirmed=stopped, evidence=evidence,
                        requires_recovery=True, release_sha256=envelope['sha256'])
        finally:
            self.lock.release()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--action', required=True)
    parser.add_argument('--measured', type=Path, required=True)
    parser.add_argument('--urdf', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    measured = read(args.measured)
    result = materialize(load_release(args.bundle), args.action, measured.get('joints', measured), LocalJoinPlanner(args.urdf))
    write(args.output, result)
    print(json.dumps({k: v for k, v in result.items() if k not in ('steps', 'final_joints')}))
