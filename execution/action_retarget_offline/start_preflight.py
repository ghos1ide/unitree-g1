#!/usr/bin/env python3
"""Check a Mode 5 LowState snapshot against both archived trajectory starts."""

import argparse
import csv
import json
from pathlib import Path
import subprocess
import sys
import tempfile

import numpy as np
from scipy.interpolate import BPoly

from retarget import STEP_NS, export_csv, file_sha, grid, write
from solver import Dynamics, HERE, NAMES, NS, RUNTIME, TargetModel

sys.path.insert(0, str(RUNTIME / 'deployment/rail_jetson'))
from mode5_locked import REST_METHOD, rest_window_passed

SLOTS = (*range(15, 29), 12, 13, 14)
VARIANTS = {'locked': 'locked_checks_passed', 'retargeted': 'retarget_checks_passed'}
UNITS = ('rad', 'rad_s', 'rad_s2')
START_TOL_RAD = .025
REST_SPEED_RAD_S = .03
REST_WINDOW_S = .15
MAX_AGE_S = .1
ALIGN_SPEED_RAD_S = .10
ALIGN_ACCEL_RAD_S2 = .40


def active_source(snapshot):
    if snapshot.get('schema') != 1 or snapshot.get('unattributed_frames') != 0:
        raise ValueError('Unsupported snapshot or unattributed LowState frames')
    active = [item for item in snapshot['sources'] if item['frames'] > 0]
    if len(active) != 1:
        raise ValueError(f'Expected one active LowState writer, received {len(active)}')
    source = active[0]
    if (source.get('host'), source.get('process')) != ('Unitree', 'basic_service'):
        raise ValueError('Active writer does not match the observed Unitree/basic_service')
    if source.get('mode_machine') != [5] or source.get('mode_pr') != [0]:
        raise ValueError('Expected Mode 5 and PR feedback throughout the observation')
    return source


def check_feedback(source, model):
    by_slot = {item['slot']: item for item in source['joints']}
    if len(by_slot) != len(SLOTS) or len(source['joints']) != len(SLOTS) or set(by_slot) != set(SLOTS):
        raise ValueError('Snapshot needs exactly motor slots 12 through 28')
    rows = [by_slot[i] for i in SLOTS]
    window = source.get('rest_window')
    raw_speed_limit = .15 if window is not None else REST_SPEED_RAD_S
    q = np.full(len(SLOTS), np.nan)
    blocked = {}
    for i, (name, row) in enumerate(zip(NAMES, rows)):
        issues = []
        if row.get('mode') != 1 or row.get('fault') != 0:
            issues.append('inactive_or_fault')
        feedback = [row.get(field, np.nan) for field in ('q', 'dq', 'vol', 'tau_est', 'max_temperature_c')]
        if np.isfinite(feedback).all():
            q[i], dq, vol, tau, temp = feedback
            if vol <= 1 or temp >= 85 or abs(tau) > min(model.effort[i], 25.):
                issues.append('voltage_temperature_or_measured_effort')
            if abs(dq) > raw_speed_limit:
                issues.append('moving')
            if not model.lower[i] <= q[i] <= model.upper[i]:
                issues.append('position_limit_margin')
        else:
            issues.append('nonfinite_or_missing_feedback')
        if issues:
            blocked[name] = issues
    age, stationary = source['age_s'], source['stationary_s']
    if window is None:  # Older saved snapshots can still be inspected offline.
        at_rest = stationary >= REST_WINDOW_S and source['stationary_ticks'] >= 3
    else:
        at_rest = rest_window_passed(window, stationary, source['stationary_ticks'])
    ready_window = np.isfinite((age, stationary)).all() and 0. <= age <= MAX_AGE_S and at_rest
    return q, blocked, bool(ready_window)


def first_point(path):
    with path.open(newline='') as handle:
        point = next(csv.DictReader(handle), None)
    if point is None or (point['kind'], point['time_ns'], point['stage_time_ns']) != ('join', '0', '0'):
        raise ValueError('First row must be the start of the reference join')
    start = np.array([[float(point[f'{joint}_{unit}']) for joint in NAMES] for unit in UNITS])
    if not np.isfinite(start).all() or np.any(np.abs(start[1:]) > 1e-9):
        raise ValueError('Trajectory must have a finite, zero-velocity and zero-acceleration start')
    return start[0]


def alignment(q0, q1, model):
    if np.any(np.minimum(q0, q1) < model.lower) or np.any(np.maximum(q0, q1) > model.upper):
        raise ValueError('Alignment endpoint is outside joint margins')
    delta = np.abs(q1 - q0)
    velocity_cap = np.minimum(model.velocity, ALIGN_SPEED_RAD_S)
    # Exact extrema of normalized rest-to-rest quintic speed and acceleration.
    duration = max(1.5, np.max(1.875 * delta / velocity_cap),
                   np.sqrt(np.max((10. / np.sqrt(3.)) * delta / ALIGN_ACCEL_RAD_S2)))
    end_ns = int(np.ceil(duration * NS / STEP_NS)) * STEP_NS
    times = grid(end_ns)
    zero = np.zeros_like(q0)
    curve = BPoly.from_derivatives([0., end_ns / NS], [[q0, zero, zero], [q1, zero, zero]])
    positions = np.stack([curve(times / NS, nu=n) for n in range(3)], axis=1)
    positions[[0, -1], 0] = [q0, q1]
    positions[[0, -1], 1:] = 0.
    peak_speed, peak_accel = np.max(np.abs(positions[:, 1:]), axis=0)
    if np.any(peak_speed > velocity_cap + 1e-9) or np.any(peak_accel > ALIGN_ACCEL_RAD_S2 + 1e-9):
        raise ValueError('Alignment preview exceeded its speed or acceleration cap')
    return times, positions


def acquire(snapshot_path, interface):
    if snapshot_path is not None:
        return json.loads(snapshot_path.read_text())
    process = subprocess.run(['bash', str(HERE / 'read_model.sh'), interface, '--snapshot'],
                             capture_output=True, text=True)
    if process.returncode != 0:
        raise ValueError(f'Read-only LowState capture failed ({process.returncode}): {process.stderr.strip()}')
    return json.loads(process.stdout)


def preflight(args):
    folder = args.trajectory_dir.resolve()
    summary = json.loads((folder / 'summary.json').read_text())
    if (summary.get('reference_only') is not False or summary.get('hardware_executable') is not False
            or tuple(summary.get('joint_names', ())) != NAMES or args.action not in summary['actions']):
        raise ValueError('Expected offline Mode 5 trajectory artifacts for this action')
    urdf = Path(summary['target_urdf'])
    if file_sha(urdf) != summary['target_urdf_sha256']:
        raise ValueError('Target URDF differs from the model used for retargeting')
    target = TargetModel(urdf)
    if target.robot.get('name') != 'g1_29dof_rev_1_0':
        raise ValueError('This preflight only compares the G1 Mode 5 model')
    out = args.output.resolve()
    if out.exists() or out == RUNTIME or RUNTIME in out.parents:
        raise ValueError('Use a new output directory outside action_runtime_v3')

    entry = summary['actions'][args.action]
    variants = ('locked',) if getattr(args, 'locked_only', False) else VARIANTS
    starts = {variant: first_point(folder / args.action / (variant + '.csv'))
              for variant in variants if entry.get(VARIANTS[variant]) is True}
    if not starts:
        raise ValueError('No requested variant passed retarget validation')
    snapshot = acquire(args.snapshot, args.interface)
    inspect_only = getattr(args, 'inspect_unconfirmed_start', False)
    if inspect_only and args.snapshot is None:
        raise ValueError('Unconfirmed-start inspection must use a saved snapshot')
    source = active_source(snapshot)
    if args.snapshot is None and source.get('rest_window', {}).get('method') != REST_METHOD:
        raise ValueError('Live probe did not supply the current Mode 5 rest window evidence')
    q, problems, ready_window = check_feedback(source, target)
    speed_crossings = {name: row['speed_exceeded_frames'] for name, slot in zip(NAMES, SLOTS)
                       for row in source['joints'] if row['slot'] == slot and row.get('speed_exceeded_frames')}
    report = dict(action=args.action, release_sha256=summary['release_sha256'], model_sha256=file_sha(urdf),
                  robot_model='g1_29dof_rev_1_0', capture='live' if args.snapshot is None else 'saved_sample',
                  read_only=True, hardware_executable=False, hardware_commands_sent=0, collision_checked=False,
                  inspect_only=inspect_only,
                  coordinate_assumption='SDK PR q interpreted as URDF angles; calibration not verified',
                  stationary_s=source['stationary_s'], age_at_capture_s=source['age_s'],
                  stationary_ticks=source['stationary_ticks'], rest_window=source.get('rest_window'),
                  window_continuity=source.get('window_continuity'),
                  longest_stationary_s=source.get('longest_stationary_s', source['stationary_s']),
                  speed_threshold_crossings=speed_crossings,
                  stationary_window_passed=ready_window, feedback_problems=problems, start_tol_rad=START_TOL_RAD,
                  matches={})
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.preflight-', dir=out.parent) as tmp:
        scratch = Path(tmp)
        artifacts = scratch / 'result'
        artifacts.mkdir()
        write(artifacts / 'snapshot.json', snapshot)
        dynamics = None
        for variant in variants:
            if variant not in starts:
                report['matches'][variant] = dict(preview_generated=False, reason='retarget checks did not pass')
                continue
            goal = starts[variant]
            distance = np.abs(q - goal)
            known = np.isfinite(distance)
            result = dict(preview_generated=False, all_axes_within_start_tol=bool(np.all(distance <= START_TOL_RAD)),
                          start_error_rad={n: float(d) for n, d, ok in zip(NAMES, distance, known) if ok},
                          outside_start_tol=[n for n, d in zip(NAMES, distance) if d > START_TOL_RAD])
            report['matches'][variant] = result
            if problems or (not ready_window and not inspect_only):
                result['reason'] = 'feedback or stationary window not confirmed'
                continue
            try:
                times, values = alignment(q, goal, target)
            except ValueError as error:
                result['reason'] = str(error)
                continue
            if dynamics is None:
                dynamics = Dynamics(scratch, target, Path('/opt/ros/foxy'))
            torque = dynamics(values)
            peak_effort = np.max(np.abs(torque), axis=0)
            if np.any(peak_effort > np.minimum(target.effort, 25.)):
                result['reason'] = 'alignment preview exceeds unloaded effort screening limit'
                result['peak_effort_nm'] = dict(zip(NAMES, peak_effort.tolist()))
                continue
            path = artifacts / (variant + '_alignment_preview.csv')
            event = dict(index=0, kind='offline_alignment_preview', start_ns=0)
            export_csv(path, [(event, times, values, torque)])
            result.update(preview_generated=True, preview_file=path.name, duration_s=int(times[-1]) / NS,
                          preview_sha256=file_sha(path), duration_ns=int(times[-1]),
                          peak_effort_nm=dict(zip(NAMES, peak_effort.tolist())))
        write(artifacts / 'preflight.json', report)
        if out.exists():
            raise FileExistsError(out)
        artifacts.rename(out)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--trajectory-dir', type=Path, required=True, help='Folder containing retarget summary.json')
    parser.add_argument('--action', default='pull')
    parser.add_argument('--locked-only', action='store_true', help='Check only the locked-waist version')
    capture = parser.add_mutually_exclusive_group()
    capture.add_argument('--interface', default='eth0')
    capture.add_argument('--snapshot', type=Path, help='Analyze stored feedback for offline testing only')
    parser.add_argument('--inspect-unconfirmed-start', action='store_true',
                        help='With --snapshot only: visualize a valid final sample without a confirmed rest window')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        report = preflight(args)
    except (KeyError, OSError, TypeError, ValueError, subprocess.CalledProcessError) as error:
        print(f'Preflight failed: {error}', file=sys.stderr)
        return 2
    print(f'Report: {args.output / "preflight.json"} (read-only, no hardware commands)')
    rest_status = 'PASS' if report['stationary_window_passed'] else 'BLOCKED'
    print(f'Rest window: {rest_status}, {report["stationary_s"]:.3f} s, {report["stationary_ticks"]} distinct ticks; '
          f'capture={report["capture"]}; inspection_only={report["inspect_only"]}')
    if report['rest_window']:
        evidence = report['rest_window']
        print(f'Max across axes: q span={evidence["q_span_rad"]:.5f}/0.003 rad; '
              f'mean |dq|={evidence["mean_abs_dq_rad_s"]:.4f}/0.03 rad/s; '
              f'peak |dq|={evidence["peak_abs_dq_rad_s"]:.4f}/0.15 rad/s')
    if not report['stationary_window_passed']:
        continuity = report['window_continuity']
        if continuity:
            fields = ('merged_ticks', 'gap_resets', 'timestamp_resets', 'tick_resets',
                      'unhealthy_frames', 'stale_tick_frames', 'invalid_samples')
            totals = ', '.join(f'{name}={continuity[name]}' for name in fields)
            print(f'Window continuity: longest={continuity["longest_window_s"]:.3f} s, {totals}')
        elif report['speed_threshold_crossings']:
            frequent = sorted(report['speed_threshold_crossings'].items(), key=lambda pair: -pair[1])[:3]
            print('Most |dq| threshold crossings: ' + ', '.join(f'{name}={count}' for name, count in frequent))
    for variant, result in report['matches'].items():
        print(f"{variant}: {result.get('preview_file', result.get('reason'))}; "
              f"axes outside start tolerance: {', '.join(result.get('outside_start_tol', [])) or 'none'}")
    return 0 if all(v['preview_generated'] for v in report['matches'].values()) else 2


if __name__ == '__main__':
    raise SystemExit(main())
