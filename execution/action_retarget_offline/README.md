# Offline three-axis waist retargeting

This tool compares two upper-body trajectories from an immutable
`action_runtime_v3` release. It does not start a runtime, read the robot, send
motor or hand commands, or produce a deployable release.

1. **Locked baseline (`locked.csv`)**: preserve archived active-arm and yaw
   positions, velocities, accelerations, and times by joint name. Set waist roll
   and pitch to constant neutral angles, with zero speed and acceleration. Keep
   the other arm at the release initialization pose.
2. **Retargeted (`retargeted.csv`)**: compute the old wrist or tool pose relative
   to the pelvis, then solve bounded IK for the active arm and yaw on the target
   model. Compare neutral and smooth roll/pitch candidates during recorded
   motion using full-tree KDL inverse dynamics. Select the feasible candidate
   with the lowest time-weighted RMS effort divided by each joint's URDF effort
   limit. Zero bias can win; the search is discrete and does not claim a global
   optimum. The other arm stays at its nominal joint pose but follows the waist
   in space. The 17 reported torques include both arms and all waist axes.

Both use the v3 gateway interpolator at 100 Hz and include every archived knot
and both sides of stage boundaries. IK is solved roughly every 100 ms, and
corrections are interpolated with zero added speed and acceleration at stage
boundaries. The final curves are screened at all output samples for pose error
(1 mm / 0.5 deg), target joint bounds with a 0.02 rad margin, speed, acceleration
and URDF effort. These are offline thresholds, not target-controller safety
limits. Time is not stretched to rescue failing candidates. A numerical
solution is not a guarantee between samples or away from the nominal start.

`report.json` includes hand-event metadata, original trajectory hashes, per-stage
candidate errors, joint-limit excess and effort. `summary.json` records the
source and target model hashes and thresholds. Column suffixes are `rad`,
`rad_s`, `rad_s2` and `nm`. The locked curve is always exported for comparison;
inspect `locked_checks_passed` before interpreting it. If any stage has no
passing retarget candidate, no `retargeted.csv` is exported for that action,
and the tool exits with status 2 after writing the reports.

## Run offline

From the workspace root, with system Python, NumPy, SciPy, `g++`, Eigen and the
already installed ROS Foxy KDL libraries (no ROS node or network needed):

```bash
python3 -B execution/action_retarget_offline/retarget.py \
  --reference-model --action push --output build/waist_reference_push
```

`--reference-model` unlocks the roll/pitch declarations in a copy of the old
gravity URDF. Its output is marked `reference_only`; this is a software test,
not a model of the new physical robot. Omit `--action` to process all actions in
the selected bundle. Use `--bundle` to compare another v3 bundle.

To check the model ID using the robot network, run `bash
execution/action_retarget_offline/read_model.sh eth0` from the workspace root.
If it reports several publishers, append `--sources` to see each DDS writer's
received frame count and model IDs, and host/process metadata when available.
The probe also reports raw feedback for motor slots 12, 13 and 14 from each
writer that sent frames. It does not verify joint zeros or control ownership,
and the converter itself does not connect to the robot.

For a live, read-only start comparison on the Mode 5 Jetson, with the robot
stationary and the offline outputs already generated:

```bash
python3 -B execution/action_retarget_offline/start_preflight.py \
  --interface eth0 --action pull \
  --trajectory-dir build/waist_retarget_mode5/session-push-9mm-0407b06c6a1e \
  --output "build/mode5_pull_preflight_$(date +%Y%m%d_%H%M%S)"
```

The command compares 17 joint positions with both first points at a 0.025 rad
position tolerance. It writes `preflight.json` and the source-attributed
`snapshot.json` to a new directory. If the only active writer is the observed
`Unitree/basic_service` and feedback is valid and fresh, a 150 ms window checks
each joint's position span (0.003 rad), time-weighted mean absolute speed
(0.03 rad/s) and raw speed peak (0.15 rad/s). The probe waits up to 20 s for a
passing window. When it passes, the script checks a rest-to-rest quintic
alignment for position, speed, acceleration and unloaded KDL effort. A passing
alignment is exported as `*_alignment_preview.csv`, not as a command. Raw
PR angles are compared to URDF coordinates without verifying calibration.
This tool does not validate collision clearance, balance, or the motor
controller's authority, and a passing report is not permission to move.
For inspection only, `--snapshot <snapshot.json> --inspect-unconfirmed-start`
also generates a preview from a valid final sample when the window fails. A
saved-sample preview is always rejected by the standalone launcher.

For the opt-in locked-waist hardware entry point and stop procedure, see
[Mode 5 locked pull](../action_runtime_v3/deployment/rail_jetson/MODE5_LOCKED_PULL.md).
The IK/ID retargeted trajectory remains offline only.

```bash
python3 -B execution/action_retarget_offline/retarget.py \
  --target-urdf /path/to/calibrated-g1.urdf --action push \
  --output build/waist_target_push
python3 -B -m unittest discover -s execution/action_retarget_offline -v
```

Supply an expanded URDF with calibrated joint origins, axes, limits and
upper-body inertias, rooted at `pelvis`, with independent yaw, roll and pitch
joints. `--neutral-roll-rad` and `--neutral-pitch-rad` are neutral angles in that
model's coordinates, not motor offsets. The pelvis frames must be aligned. The
default pose target is the active `*_wrist_yaw_link`. For a calibrated tool
comparison, provide `--source-frame` and `--target-frame` for one `--action`.
Use frames rigidly attached to the wrist for this comparison: any unrecorded
finger DOFs are nominal and not part of contact tracking.

KDL assumes a fixed pelvis, upright gravity, nominal unrecorded joints and
zero external wrenches. It does not model changing payloads, friction, balance,
collisions, finger synchronization in physics, or fixture contact. The first
stage is the archived reference join, not a plan from measured state. Before
hardware integration, confirm the new waist's joint-to-actuator mapping and
zero offsets, calibrate tool and fixture frames, and validate clearance and
loads in simulation. The v3 task trajectory interface still only accepts yaw.
