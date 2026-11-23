#!/usr/bin/env python3
"""Replay archived joint trajectories and dry-run the v3 sequence in memory."""

import argparse
import csv
from datetime import datetime
import hashlib
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import warnings

import numpy as np


EXECUTION = Path(__file__).resolve().parents[1]
RUNTIME = EXECUTION / 'action_runtime_v3'
GATEWAY = RUNTIME / 'native_ws/src/g1_unitree_control/src/arm_sdk_hardware_gateway.cpp'
HARNESS = EXECUTION / 'rail_migration_offline_v1/interpolation_check.cpp'
URDF = RUNTIME / 'assets/planner.urdf'
STEP_NS = 10_000_000
NS = 1_000_000_000
FIELDS = ('positions', 'velocities', 'accelerations')
UNITS = ('rad', 'rad_s', 'rad_s2')
TOLERANCE = dict(zip(FIELDS, (1e-10, 1e-8, 1e-7)))

sys.path.insert(0, str(RUNTIME / 'deployment/rail_jetson'))
from bundle import digest, write
from executor import LocalJoinPlanner, SequenceExecutor, hand_step_policy
from offline_transport import OfflineTransport
from releases import load_release


def file_sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def compile_sampler(folder):
    source = GATEWAY.read_bytes()
    start = source.index(b'  TrajectorySample interpolate_locked(double elapsed) const\n')
    end = source.index(b'  void publish_preview_locked(', start)
    method = source[start:end]
    (folder / 'interpolation.inc').write_bytes(method)
    harness = folder / HARNESS.name
    shutil.copyfile(HARNESS, harness)
    binary = folder / 'interpolation_check'
    subprocess.run(['g++', '-std=c++17', '-O2', str(harness), '-o', str(binary)], check=True)
    return binary, hashlib.sha256(method).hexdigest()


def grid(end_ns):
    return np.append(np.arange(0, end_ns, STEP_NS, dtype=np.int64), end_ns)


def sample_arm(binary, trajectory):
    names, points = trajectory['joint_names'], trajectory['points']
    stamps = [p['time_from_start'] for p in points]
    knots = np.asarray([s['sec'] * NS + s['nanosec'] for s in stamps], dtype=np.int64)
    if knots[0] != 0 or np.any(np.diff(knots) <= 0):
        raise ValueError('Archived arm times must start at zero and increase')
    times = np.union1d(grid(knots[-1]), knots)
    original = np.asarray([[v for field in FIELDS for v in p[field]] for p in points])
    with io.StringIO() as request:
        request.write(f'1\n{len(names)} {len(knots)} {len(times)}\n')
        np.savetxt(request, np.column_stack((knots / NS, original)), fmt='%.17g')
        np.savetxt(request, times / NS, fmt='%.17g')
        output = subprocess.run([str(binary)], input=request.getvalue(), text=True,
                                stdout=subprocess.PIPE, check=True).stdout
    values = np.loadtxt(io.StringIO(output), ndmin=2)
    if values.shape != (len(times), original.shape[1]) or not np.isfinite(values).all():
        raise ValueError('Invalid offline sampler output')
    errors = np.abs(values[np.searchsorted(times, knots)] - original)
    maxima = errors.reshape(len(knots), len(FIELDS), len(names)).max(axis=(0, 2))
    if any(error > limit for error, limit in zip(maxima, TOLERANCE.values())):
        raise ValueError(f'Gateway samples do not match archived knots: {maxima}')
    return times, values, dict(zip(FIELDS, maxima.tolist()))


def dry_run(envelope, name, planner):
    action = envelope['payload']['actions'][name]
    generated = []

    def plan_join(measured, target, parameters, velocity, acceleration):
        trajectory = planner(measured, target, parameters, velocity, acceleration)
        generated.append(trajectory)
        return trajectory

    transport = OfflineTransport(step_seconds=0)
    executor = SequenceExecutor(transport, plan_join)
    initial = transport.initialize(envelope, executor.check)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always', RuntimeWarning)
        result = executor.execute(envelope, name)
    if result['status'] != 'SUCCEEDED':
        raise RuntimeError(f"Offline sequence failed for {name}: {result.get('message')}")
    expected = [s['trajectory_sha256'] for s in action['program'] if s['kind'] == 'recorded']
    actual = [c['trajectory_sha256'] for c in transport.calls if c['kind'] == 'recorded']
    if actual != expected:
        raise ValueError('Offline sequence altered recorded trajectories')
    return dict(result, initialization=initial, calls=transport.calls,
                generated_runtime_joins=generated, final_joints=transport.joints,
                warnings=sorted({str(w.message) for w in caught}), hardware_commands_sent=0)


def plot_curves(path, version, action, names, timeline):
    from matplotlib import pyplot as plt

    fig, axes = plt.subplots(3, 1, figsize=(12, 8), sharex=True)
    colors = plt.rcParams['axes.prop_cycle'].by_key()['color']
    labels = [n.replace('left_', '').replace('right_', '').replace('_joint', '').replace('_', ' ')
              for n in names]
    for axis, label in zip(axes, ('Position (rad)', 'Velocity (rad/s)', 'Acceleration (rad/s^2)')):
        axis.set_ylabel(label)
        axis.grid(alpha=.25)
    for idx, (stage, times, values) in enumerate(timeline):
        t = (stage['start_ns'] + times) / NS
        for k, axis in enumerate(axes):
            for j, label in enumerate(labels):
                axis.plot(t, values[:, k * len(names) + j], color=colors[j], lw=1.1,
                          label=label if idx == 0 else None)
            if stage['kind'] != 'recorded':
                color = '#dce6ef' if stage['kind'] == 'join' else '#f8e3c0'
                axis.axvspan(t[0], t[-1], color=color, alpha=.7, zorder=0)
        axes[0].text((t[0] + t[-1]) / 2, 1.015, str(stage['index']), ha='center',
                     transform=axes[0].get_xaxis_transform(), fontsize=8)
    axes[-1].set(xlabel='Archived time (s)', xlim=(0, timeline[-1][0]['end_ns'] / NS))
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc='lower center', ncol=4, frameon=False, fontsize=9)
    fig.suptitle(f'{action}\n{version} | archived joint replay', y=.985, fontsize=13)
    fig.tight_layout(rect=(0, .07, 1, .925))
    fig.savefig(path, dpi=125)
    plt.close(fig)


def replay_action(folder, binary, envelope, name, planner):
    action = envelope['payload']['actions'][name]
    archived = action['plan']['segments']
    program = action['program']
    payload = envelope['payload']
    names = archived[0]['trajectory']['joint_names']
    offset = 0
    current = None
    timeline = []
    for i, (segment, step) in enumerate(zip(archived, program), 1):
        kind, trajectory = step['kind'], segment['trajectory']
        detail = dict(index=i, kind=kind, label=step['label'], start_ns=offset)
        if kind == 'hand':
            if current is None:
                raise ValueError('No arm pose available for the archived hand stage')
            times = grid(round(step['duration'] * NS))
            values = np.zeros((len(times), len(FIELDS) * len(names)))
            # Archived hand-stage joint arrays can be placeholders in optimized exports.
            values[:, :len(names)] = current
            policy = hand_step_policy(action, step)
            detail['hand'] = {k: policy[k] for k in ('target_q', 'hand_target_required',
                                                     'finger_sequence') if k in policy}
        else:
            expected = step['reference_trajectory_sha256' if kind == 'join' else 'trajectory_sha256']
            if digest(trajectory) != expected:
                raise ValueError('Archived arm trajectory hash mismatch')
            if trajectory['joint_names'] != names:
                raise ValueError('Joint order changed within an archived action')
            times, values, errors = sample_arm(binary, trajectory)
            if current is not None:
                delta = float(np.max(np.abs(current - values[0, :len(names)])))
                if delta > 1e-7:
                    raise ValueError(f'Armed reference discontinuity at stage {i}: {delta} rad')
                detail['start_delta_rad'] = delta
            current = values[-1, :len(names)]
            detail.update(trajectory_sha256=expected, knots=len(trajectory['points']),
                          max_knot_error=errors)
        offset += int(times[-1])
        detail.update(end_ns=offset, samples=len(times))
        timeline.append((detail, times, values))

    runtime = dry_run(envelope, name, planner)
    folder.mkdir(parents=True)
    with (folder / 'samples.csv').open('w', newline='') as handle:
        writer = csv.writer(handle)
        writer.writerow(['stage', 'kind', 'time_ns', 'stage_time_ns'] +
                        [f'{joint}_{unit}' for unit in UNITS for joint in names])
        for stage, times, values in timeline:
            for time_ns, row in zip(times, values):
                writer.writerow([stage['index'], stage['kind'], stage['start_ns'] + int(time_ns),
                                 int(time_ns), *row])
    plot_curves(folder / 'trajectory.png', payload['version'], name, names, timeline)
    stages = [stage for stage, _, _ in timeline]
    summary = dict(action=name, arm=action['plan']['arm'], duration_s=offset / NS,
                   recorded_segments=sum(s['kind'] == 'recorded' for s in stages),
                   arm_knots=sum(s.get('knots', 0) for s in stages),
                   samples=sum(s['samples'] for s in stages),
                   runtime_status=runtime['status'], generated_joins=len(runtime['generated_runtime_joins']),
                   runtime_warnings=runtime['warnings'],
                   max_knot_error={field: max(s.get('max_knot_error', {}).get(field, 0.0)
                                              for s in stages) for field in FIELDS})
    write(folder / 'trajectory.json', dict(summary, release_sha256=envelope['sha256'],
          source=action['source'], source_plan_sha256=action['source_plan_sha256'],
          source_verification=action['verification'], hardware_executable=False,
          hardware_commands_sent=0, archived_segments_sha256=digest(archived),
          archived_segments=archived, stages=stages, runtime_dry_run=runtime))
    print(f"{payload['version']}/{name}: {summary['samples']} samples; {runtime['status']}", flush=True)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path(__file__).resolve().parent)
    out = parser.parse_args().output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    if (out / 'summary.json').exists():
        parser.error('Output already contains a completed replay; choose another directory')
    releases = [RUNTIME / 'config/selected_bundle.json',
                *sorted((RUNTIME / 'data/action_releases').glob('*/bundle.json'))]
    model_sha = file_sha(URDF)
    planner = LocalJoinPlanner(URDF)
    with tempfile.TemporaryDirectory(prefix='.replay-', dir=out) as tmp:
        scratch = Path(tmp)
        os.environ.update(MPLBACKEND='Agg', MPLCONFIGDIR=str(scratch / 'matplotlib'))
        binary, method_sha = compile_sampler(scratch)
        versions = []
        for path in releases:
            input_sha = file_sha(path)
            envelope = load_release(path)
            payload = envelope['payload']
            version = payload['version']
            if (out / version).exists() or (scratch / version).exists():
                raise FileExistsError(f'Release output already exists: {version}')
            actions = payload['actions']
            if any(a['identity'].get('urdf_sha256', a['identity'].get('robot_sha256')) != model_sha
                   for a in actions.values()):
                raise ValueError(f'Planner URDF does not match release {version}')
            results = [replay_action(scratch / version / name, binary, envelope, name, planner)
                       for name in actions]
            if file_sha(path) != input_sha:
                raise ValueError(f'Input changed during replay: {path}')
            versions.append(dict(version=version, source=str(path.relative_to(EXECUTION)),
                                 file_sha256=input_sha, payload_sha256=envelope['sha256'],
                                 integrity_verified=True, actions=results))
        report = dict(status='SUCCEEDED', completed_at=datetime.now().astimezone().isoformat(),
                      simulation=True, hardware_commands_sent=0, collision_checked=False,
                      sampling_step_ns=STEP_NS, includes_all_arm_knots=True, knot_tolerance=TOLERANCE,
                      gateway_interpolation_sha256=method_sha, harness_sha256=file_sha(HARNESS),
                      planner_urdf_sha256=model_sha, releases=versions)
        write(scratch / 'summary.json', report)
        for result in versions:
            version = result['version']
            (scratch / version).rename(out / version)
        (scratch / 'summary.json').rename(out / 'summary.json')


if __name__ == '__main__':
    main()
