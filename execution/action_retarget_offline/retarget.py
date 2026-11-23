#!/usr/bin/env python3
"""Compare locked and task-space retargeted waist trajectories without robot I/O."""

import argparse
import csv
from pathlib import Path
import sys
import tempfile

import numpy as np

from solver import (ACCEL_CAP, HERE, IK_STEP_NS, LIMIT_MARGIN_RAD, NAMES, NS, POSE_TOL_M,
                    POSE_TOL_RAD, RUNTIME, SIZE, WAIST, Dynamics, OfflineModel, PoseMatcher,
                    TargetModel, evaluate, reference_urdf)

sys.path.insert(0, str(HERE.parent / 'action_replay_offline_20261119'))
from replay import (STEP_NS, URDF, compile_sampler, digest, file_sha, grid, hand_step_policy,
                    load_release, sample_arm, write)

SUFFIXES = ('rad', 'rad_s', 'rad_s2', 'nm')
CONTINUITY_TOL = np.array([1e-7, 1e-6, 1e-5])


def chain_contains(model, frame, names, *, nominal=()):
    if frame not in {link.get('name') for link in model.robot.findall('link')}:
        raise ValueError(f'No model frame {frame}')
    found = set()
    link = frame
    while link in model.joints:
        parent, kind, _, _, _, name, _, _ = model.joints[link]
        if kind != 'fixed':
            found.add(name)
        link = parent
    if link != 'pelvis' or not set(names) <= found or found - set(names) - set(nominal):
        raise ValueError(f'{frame} is not driven by the expected pelvis/arm chain')


def continuity(previous, current):
    if previous is not None and np.any(np.max(np.abs(previous - current), axis=1) > CONTINUITY_TOL):
        raise ValueError('Discontinuous stage boundary in position, velocity or acceleration')


def stages(action, init, sampler, neutral):
    offset, previous = 0, None
    side = action['plan']['arm']
    moving = [i for i, name in enumerate(NAMES) if name.startswith(side + '_')] + [SIZE - 3]
    expected = {NAMES[i] for i in moving}
    pose = np.array([init[n] for n in NAMES[:-2]] + list(neutral))

    for index, (step, segment) in enumerate(zip(action['program'], action['plan']['segments']), 1):
        kind, trajectory = step['kind'], segment['trajectory']
        if step['side'] != side:
            raise ValueError('Only single active arm actions are supported')
        event = dict(index=index, label=step['label'], kind=kind, side=side, start_ns=offset)
        if kind == 'hand':
            if previous is None:
                raise ValueError('Missing initial arm pose for hand hold')
            policy = hand_step_policy(action, step)
            times = grid(round(step['duration'] * NS))
            values = np.zeros((len(times), 3, SIZE))
            values[:, 0, :] = previous[0]
            event['hand'] = {k: policy[k] for k in ('target_q', 'hand_target_required', 'finger_sequence',
                                                    'hand_completion_policy') if k in policy}
        else:
            if kind not in ('recorded', 'join'):
                raise ValueError(f'Unsupported stage: {kind}')
            key = 'reference_trajectory_sha256' if kind == 'join' else 'trajectory_sha256'
            names = trajectory['joint_names']
            if set(names) != expected or len(names) != len(expected) or digest(trajectory) != step[key]:
                raise ValueError(f'Invalid archived trajectory: {step["label"]}')
            times, rows, knot_error = sample_arm(sampler, trajectory)
            values = np.zeros((len(times), 3, SIZE))
            values[:, 0, :] = pose
            values[:, :, [NAMES.index(name) for name in names]] = rows.reshape(len(times), 3, -1)
            event.update(source_trajectory_sha256=step[key], max_source_knot_error=knot_error)
            event.update({k: step[k] for k in ('recorded_hand', 'delayed_hand') if k in step})
        continuity(previous, values[0])
        previous = values[-1]
        offset += int(times[-1])
        yield dict(event, end_ns=offset, samples=len(times)), times, values, moving


def export_csv(path, rows):
    with path.open('w', newline='') as handle:
        writer = csv.writer(handle)
        writer.writerow(['stage', 'kind', 'time_ns', 'stage_time_ns'] +
                        [f'{n}_{unit}' for unit in SUFFIXES for n in NAMES])
        for event, times, values, torque in rows:
            for t, row, tau in zip(times, values.reshape(len(times), -1), torque):
                writer.writerow([event['index'], event['kind'], event['start_ns'] + int(t), int(t), *row, *tau])


def solve_action(folder, action, init, old, target, frames, sampler, dynamics, options):
    source_frame, target_frame = frames
    previous = None
    locked_rows, adaptive_rows = [], []
    reports = []
    # Boundary IK is deterministic and reused across adjacent stages to keep a hand hold stationary.
    previous_boundary = None
    for event, times, values, moving in stages(action, init, sampler, options.neutral):
        source_names = [NAMES[i] for i in moving]
        if not reports:
            chain_contains(old, source_frame, source_names,
                           nominal={j[5] for j in old.joints.values()} - set(NAMES))
            chain_contains(target, target_frame, source_names + list(WAIST[1:]), nominal=target.nominal)
            matcher = PoseMatcher(target, target_frame, moving)
        poses = old.batch_transforms([source_frame], {}, NAMES, values[:, 0, :])[source_frame]
        locked, torque = evaluate(times, values, poses, target, target_frame, dynamics)
        locked_rows.append((event, times, values, torque))
        start = (matcher.correction(values[0, 0], poses[0]) if previous_boundary is None
                 else previous_boundary)
        end = start if event['kind'] == 'hand' else matcher.correction(values[-1, 0], poses[-1])

        candidates = [(0., 0.)]
        a = options.waist_bias_rad
        if event['kind'] == 'recorded' and a:
            candidates.extend([(a, 0.), (-a, 0.), (0., a), (0., -a)])
        best, considered = None, []
        for amplitude in candidates:
            if event['kind'] == 'hand':
                candidate = values.copy()
                candidate[:, 0, moving] += start
            else:
                candidate = matcher.curve(times, values, poses, amplitude, (start, end))
            if np.array_equal(candidate, values):
                metrics, candidate_torque = locked, torque
            else:
                metrics, candidate_torque = evaluate(times, candidate, poses, target, target_frame, dynamics)
            considered.append(dict(amplitude_rad=list(amplitude), **metrics))
            cost = metrics['rms_effort_fraction']
            if metrics['accepted'] and (best is None or cost + 1e-6 < best[0]):
                best = (cost, amplitude, candidate, candidate_torque)
        if best is None:
            previous = None
        else:
            _, _, candidate, torque = best
            continuity(previous, candidate[0])
            previous = candidate[-1]
            adaptive_rows.append((event, times, candidate, torque))
        previous_boundary = end
        reports.append(dict(event, locked=locked, adaptive_candidates=considered,
                            selected_amplitude_rad=None if best is None else list(best[1])))
        print(f"  {event['index']}: {event['label']}: locked={locked['accepted']}, "
              f"retarget={None if best is None else best[1]}", flush=True)

    folder.mkdir()
    if not reports:
        raise ValueError('No archived stages')
    export_csv(folder / 'locked.csv', locked_rows)
    solved = len(adaptive_rows) == len(reports)
    if solved:
        export_csv(folder / 'retargeted.csv', adaptive_rows)
    result = dict(source=action['source'], source_plan_sha256=action['source_plan_sha256'],
                  source_verification=action['verification'], arm=action['plan']['arm'],
                  frames=dict(source=source_frame, target=target_frame),
                  locked_checks_passed=all(s['locked']['accepted'] for s in reports),
                  retarget_checks_passed=solved, samples=sum(s['samples'] for s in reports),
                  hardware_executable=False, hardware_commands_sent=0, stages=reports)
    write(folder / 'report.json', result)
    return {k: v for k, v in result.items() if k != 'stages'}


def run(options):
    bundle_path = options.bundle.resolve()
    release = load_release(bundle_path)
    payload = release['payload']
    actions = options.action or list(payload['actions'])
    if set(actions) - payload['actions'].keys() or len(set(actions)) != len(actions):
        raise ValueError('Unknown or repeated action selection')
    source_sha = file_sha(URDF)
    for name in actions:
        identity = payload['actions'][name]['identity']
        if identity.get('urdf_sha256', identity.get('robot_sha256')) != source_sha:
            raise ValueError(f'Old planner URDF differs from action {name}')
    out = options.output.resolve()
    if out.exists() or out == RUNTIME or RUNTIME in out.parents:
        raise ValueError('Choose a new output directory outside action_runtime_v3')
    out.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix='.retarget-', dir=out.parent) as tmp:
        scratch = Path(tmp)
        artifact = scratch / 'result'
        artifact.mkdir()
        if options.reference_model:
            target_path = artifact / 'reference_only.urdf'
            reference_urdf(target_path)
        else:
            target_path = options.target_urdf.resolve()
        target_sha = file_sha(target_path)
        source = OfflineModel(URDF)
        target = TargetModel(target_path)
        if (np.any(options.neutral < target.lower[-2:]) or np.any(options.neutral > target.upper[-2:])):
            raise ValueError('Neutral waist angles outside target position margins')
        sampler, interpolation_sha = compile_sampler(scratch)
        dynamics = Dynamics(scratch, target, options.ros_prefix)
        results = {}
        for name in actions:
            action = payload['actions'][name]
            wrist = action['plan']['arm'] + '_wrist_yaw_link'
            frames = options.source_frame or wrist, options.target_frame or wrist
            print(f'{payload["version"]}/{name}', flush=True)
            results[name] = solve_action(artifact / name, action, payload['initialization']['target'],
                                         source, target, frames, sampler, dynamics, options)
        if file_sha(target_path) != target_sha or file_sha(URDF) != source_sha:
            raise ValueError('URDF changed during offline processing')
        report = dict(version=payload['version'], release_sha256=release['sha256'], bundle=str(bundle_path),
                      old_urdf_sha256=source_sha, target_urdf_sha256=target_sha,
                      target_urdf=(target_path.name if options.reference_model else str(target_path)),
                      reference_only=options.reference_model, gateway_interpolation_sha256=interpolation_sha,
                      pose_frame_root='pelvis', hardware_executable=False, hardware_commands_sent=0,
                      hardware_read=False, collision_checked=False, contact_modelled=False,
                      dynamics='KDL RNE; fixed pelvis; gravity -9.81 m/s^2 along Z; zero external wrenches',
                      nominal_unrecorded_joints=target.nominal, joint_names=NAMES,
                      neutral_waist_rad=dict(zip(WAIST[1:], options.neutral.tolist())),
                      pos_margin_rad=LIMIT_MARGIN_RAD, pos_limits_rad=dict(zip(NAMES, np.stack(
                          [target.lower, target.upper], axis=1).tolist())),
                      velocity_caps_rad_s=dict(zip(NAMES, target.velocity.tolist())),
                      effort_caps_nm=dict(zip(NAMES, target.effort.tolist())), accel_cap_rad_s2=ACCEL_CAP,
                      max_pose_error_m=POSE_TOL_M, max_pose_error_rad=float(POSE_TOL_RAD),
                      sample_step_ns=STEP_NS, ik_step_ns=IK_STEP_NS, actions=results)
        write(artifact / 'summary.json', report)
        if out.exists():
            raise FileExistsError(out)
        artifact.rename(out)
    return all(r['retarget_checks_passed'] for r in results.values())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, default=RUNTIME / 'config/selected_bundle.json')
    parser.add_argument('--action', action='append', help='Convert this action; repeat to select several')
    model = parser.add_mutually_exclusive_group(required=True)
    model.add_argument('--target-urdf', type=Path, help='Calibrated target with three revolute waist axes')
    model.add_argument('--reference-model', action='store_true', help='Unlock old URDF only for offline tests')
    parser.add_argument('--source-frame')
    parser.add_argument('--target-frame')
    parser.add_argument('--neutral-roll-rad', type=float, default=0.)
    parser.add_argument('--neutral-pitch-rad', type=float, default=0.)
    parser.add_argument('--waist-bias-rad', type=float, default=.05, help='Candidate peak excursion (0 to .15)')
    parser.add_argument('--ros-prefix', type=Path, default=Path('/opt/ros/foxy'), help='Local KDL headers/libraries')
    parser.add_argument('--output', type=Path, required=True)
    options = parser.parse_args()
    if ((options.source_frame or options.target_frame) and
            (not (options.source_frame and options.target_frame) or len(options.action or []) != 1)):
        parser.error('Custom frame comparison requires --source-frame, --target-frame and one --action')
    options.neutral = np.asarray([options.neutral_roll_rad, options.neutral_pitch_rad])
    if not np.isfinite(options.neutral).all() or not 0. <= options.waist_bias_rad <= .15:
        parser.error('Non-finite neutral or out-of-range waist candidate amplitude')
    return 0 if run(options) else 2


if __name__ == '__main__':
    raise SystemExit(main())
