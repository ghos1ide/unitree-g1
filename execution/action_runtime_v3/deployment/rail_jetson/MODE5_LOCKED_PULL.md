# Mode 5 locked pull commissioning

This profile accepts only `initialize` and the selected `pull`. The preparation
is a slow, reviewed 17-axis quintic to the archived start. Replay keeps roll and
pitch at 0 rad and uses the original right-arm and waist-yaw curves and right
hand sequence. Any runtime join still uses measured feedback. The selected
bundle, legacy configuration, and installed legacy gateway are unchanged.

## Before motion

- Confirm the Mode 5 PR mapping, including the sign and zero of slots 13 and 14.
  The profile assumes calibrated neutral is 0 rad on both slots.
- Support the robot in stationary motion control and check the full swept path
  of both arms and waist, including the fixture, fingers and cables. The URDF
  check here is not a collision or balance check. Use an unloaded, contact-free
  first test with a second operator able to stop it immediately.
- Verify that the existing right-hand `hand_service` is already ready and owns
  the correct device. Do not start it as a diagnostic. Stop WSL/other arm writers;
  leave `humanoid`, `ai_sport`, `basic_service` and `emergency_stop` running.
- Never reuse a saved or failing preflight or launch the legacy profile as a
  workaround. Re-run the preflight after a change to the pose or setup.

## Check Right Hand Feedback

Before submitting `initialize`, check the RH56 RPC discovery and read fresh
feedback from only the right hand. This works while the standalone API is up.

```bash
J=execution/action_runtime_v3/deployment/rail_jetson
bash "$J/run_standalone.sh" --inspect-right-hand
```

`Right hand feedback ready` confirms that the existing `HandClient` received
valid feedback. If both RPC counts are zero and the read times out with 3104,
check where `hand_service` is running and which interface it uses. When the
service responds with offline feedback or a serial error, verify the right-hand
device path, port ownership and hand power. Current project defaults use the
old FTDI device; map any replacement adapter from actual right-hand wiring.
For the WCH dual-port adapter, see the [hand service startup options](../../../../docs/hand_service_api.md)
and bind only the confirmed right-hand USB interface with `--right-usb-port`.

If an earlier initialization failed before arming, wait for right-hand feedback
and inspect `robot status`. With `armed=false`, `command_publisher_exists=false`
and `requiresRecovery=true`, clear the task latch using
`robot recover --contact-clear --support-ready` after confirming the stated
conditions. Run `robot status` again before initiating motion.

## Hand Off The Arm SDK

With the robot stationary and supported and the physical stop ready, run on
Jetson. This uses RobotState ServiceSwitch for `g1_arm_example` only. It does
not create an arm command publisher or switch the motion-control mode.

```bash
cd ~/rh56dfx/workspace
R=execution/action_runtime_v3
J="$R/deployment/rail_jetson"
bash "$J/run_standalone.sh" --mode5-arm-service stop --confirm-mode5-clear
```

Proceed on `Mode 5 handoff clear`. The command verifies that `g1_arm_example`
is stopped, the onboard `Unitree/humanoid` reader is still present, and there
are no `/arm_sdk` writers. If the writer remains after the service stops, it
still blocks startup. Inspect both views without starting any controller:

```bash
bash "$J/run_standalone.sh" --inspect-mode5-arm-sdk
bash "$J/run_standalone.sh" --inspect-robot-services
```

ServiceList shows service state. DDS evidence shows the host, process and PID
advertised by a task endpoint. If a separate writer remains, identify it on
the reported host and stop the confirmed competing app normally.

## Capture The Start

From the workspace root on Jetson, choose a new directory and collect feedback:

```bash
P="build/mode5_pull_live_$(date +%Y%m%d_%H%M%S)"
python3 -B execution/action_retarget_offline/start_preflight.py \
  --interface eth0 --action pull --locked-only \
  --trajectory-dir build/waist_retarget_mode5/session-push-9mm-0407b06c6a1e \
  --output "$P"
```

The read-only probe waits up to 20 s for a qualifying window. Check for
`Rest window: PASS` and `locked: locked_alignment_preview.csv`. The 150 ms
window requires each axis to stay within 0.003 rad of its own minimum and
limits mean and peak absolute speed to 0.03 and 0.15 rad/s. A fresh capture
using this method is required; previous reports cannot be used. Review the
locked CSV path, not the retargeted path. Axes
outside direct-start tolerance can be expected: the preview is the preparation
that moves those joints. The live launch never sets aside the start comparison.

```bash
bash "$J/run_standalone.sh" --hardware --check-only --mode5-preflight "$P/preflight.json"
```

## Start and replay

Only after the clearance, support, calibration, and stop checks above, use the
same path in a foreground terminal. This starts passive services on loopback,
without arming or moving. It creates separate local credentials under
`data/mode5_locked`. It requires the built Mode 5 gateway and original HandClient
adapter (build commands below).

```bash
bash "$J/run_standalone.sh" --hardware \
  --mode5-preflight "$P/preflight.json" --confirm-mode5-clear
```

From a second terminal in the workspace, define the request command and check
that `connected=true`, `phase=DETACHED`, and `availableActions=["pull"]`:

```bash
R=execution/action_runtime_v3
robot() {
  env -u ROBOT_API_KEY python3 -B "$R/deployment/rail_jetson/robot_request.py" "$@" \
    --base http://127.0.0.1:18795 --key-file "$R/data/mode5_locked/api.key"
}
robot status
```

Check that `mode5_preparation.preflight` is the reviewed file and
`mode5_preparation.start_comparison.matches` is `true`. A null comparison
means feedback is missing or older than 100 ms. The signed joint offsets
compare the current pose with the first row of the reviewed alignment, using
a tolerance of 0.050 rad per axis. The gateway keeps a separate 0.010 rad
limit between its held reference and measured pose. Include the blend from
the held pose to the reviewed start when checking the swept path. The worker
loads the preflight once at launch; creating another preflight does not update
it. If the robot pose no longer matches, confirm idle, silent output (release
with support if still armed), exit the old stack, then capture, review and
launch with a new preflight. If the new stack reports `requiresRecovery=true`,
use `robot recover --contact-clear --support-ready` before initializing.

**The next two requests cause motion.** Send `robot initialize` first. It moves
both arms and all three waist axes. Wait for `SUCCEEDED` and `ARMED_HOLD` with
`robot status`; only then send `robot pull`. That request moves the right arm,
waist yaw and right hand. The controller can slow or stop on tracking errors.
It does not change the stored curves.

Mode 5 replay start alignment uses the gateway's 0.050 rad limit per trajectory
axis. Endpoint settling for `initialize` and `pull` uses the planning limit of
2.8 degrees (0.04887 rad) per axis. Roll and pitch still target 0 rad; each must
be within 0.040 rad to admit replay. After preparation, the gateway enters
damped recovery above 0.050 rad on either axis. Include the admitted waist
offsets and start blends in the swept-path clearance review.

For a controlled task stop, use `robot stop` from a second terminal. Do not treat
STOP, closing the client or Ctrl-C as release of output. For an immediate stop,
use the verified physical stop procedure. Before shutdown or controller handoff,
unload and support the arms, request `robot release --contact-clear --support-ready`,
and confirm released output. Do not send another task after an uncertain result.

After `robot release` succeeds and the standalone supervisor and gateway have
exited, restore the original onboard arm action service:

```bash
bash "$J/run_standalone.sh" --mode5-arm-service restore --confirm-mode5-clear
```

## Software checks

From the workspace root, with `R` and `J` set as above:

```bash
cmake --build "$R/native_ws/build/g1_unitree_control" --target arm_sdk_hardware_gateway -j2
bash "$J/build_native_helpers.sh"
source /opt/ros/foxy/setup.bash
source "$R/native_ws/install/setup.bash"
"$R/native_ws/build/g1_unitree_control/arm_sdk_hardware_gateway" --check-mode5-locked
```

The check creates no ROS node. Native DDS and physical motion still require
commissioning on the robot; the software checks are not physical validation.
