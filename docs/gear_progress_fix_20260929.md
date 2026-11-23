# GEAR progress repair, 2026-09-29

## Behavior

GEAR previously emitted business progress only after a whole contact block
(up to eight cycles) finished. The task with count=3, preAction=NONE and
cycleMode=FINAL_RETRACT_ONLY therefore went directly from 0 to 3.

The existing continuous trajectories and action submissions are preserved.
Interior cycle checkpoints now follow the native controller's trajectory clock,
including its tracking-related slowdown, pauses and initial blending. Feedback
publishes desired.time_from_start; the Python transport consumes it during its
existing wait loop. No new sleep, settle, stationary check, action goal or
confirmation wait was added. ROS feedback callbacks only update the clock;
business events are emitted on the existing execution thread.

PreAction does not count. FULL_CYCLE counts at the recorded return to the lower
position. FINAL_RETRACT_ONLY counts its last retraction once. Existing block-end
success/settle milestones remain in place. Cancellation or failure retains only
the counts already reported. These are recorded-motion counts, not external
lock-signal verification.

GEAR publishes the last count increment and SUCCEEDED together, only after the
recorded return to the initialization position and its existing endpoint check
succeed. Earlier counts still advance normally. During grip release and return,
the task stays RUNNING with progress=count-1. For count=5 the last change is
(RUNNING, 4) -> (SUCCEEDED, 5); for count=1 it is (RUNNING, 0) -> (SUCCEEDED, 1).
There is never a new GEAR (RUNNING, count) callback. Both fields are committed in
one task transition and one callback. No extra check, sleep or trajectory was
added. Both installed exit trajectories end at the initialization target (maximum
numerical difference below 2e-15 radians). NWS completion behavior is unchanged.

activeTaskId stays set through the return, and new tasks are rejected as busy
(2001). Return failure, cancellation or restart prevents success and does not add
the deferred last count (count-1, or zero for a single cycle). The controller's
internal motion count is separate from this published completion count. The
postCompletion query/storage support is retained only for
historical records created by the earlier completion policy; new tasks do not
use it or declare success before returning.

## Files

- execution/action_runtime_v3/deployment/rail_jetson/gear_protocol.py
- execution/action_runtime_v3/deployment/rail_jetson/gear_progress.py
- execution/action_runtime_v3/deployment/rail_jetson/executor.py
- execution/action_runtime_v3/deployment/rail_jetson/ros_transport.py
- execution/action_runtime_v3/deployment/rail_jetson/offline_transport.py
- execution/action_runtime_v3/deployment/rail_jetson/test_gear_progress.py
- execution/action_runtime_v3/native_ws/src/g1_unitree_control/src/arm_sdk_hardware_gateway.cpp
- communication/eln_http_demo/robot_gateway/manager.py
- communication/eln_http_demo/robot_gateway/store.py
- communication/eln_http_demo/tests/test_gear_completion.py

## Verification

- Project Release configure and build: passed.
- Native arm_sdk_hardware_gateway build: passed.
- Project CTest: 5/5 passed.
- Offline GEAR progress tests: 6/6 passed. Covers all four modes, counts 1/3/9/17,
  original trajectory blocks and arrays, large-count lazy generation, paused and
  repeated feedback, failure/cancellation, and progress within one motion goal.
- Reliable callback regression tests: 9/9 passed.
- GEAR final-count, gateway and reliable-callback regressions: 52/52 passed.
  Covers simultaneous final count/success after return, counts 1 and 5 in all
  GEAR modes, busy admission, no last increment on return failure/cancel/exception,
  restart and historical DB migration. Project CTest was rerun: 5/5 passed.
- Hardware reads: existing service health/state only; no direct hardware access.
- Hardware motion tests: none.

## Activation

The compiled gateway was installed atomically at the existing repository-local
native_ws/install/g1_unitree_control/lib/g1_unitree_control/arm_sdk_hardware_gateway
path. The previous binary is backed up at
build/gear-progress-backup-20260929/arm_sdk_hardware_gateway.

The final-count correction was loaded by restarting the execution service after
confirming no active task and a DETACHED controller. New API PID: 28561; worker
PID: 28446; supervisor PID: 28336. Both ports 8795 and 8090 pass health checks.
The robot remains uninitialized with recovery required; no recovery,
initialization or motion command was issued. Hardware validation must follow the
project's warning, workspace, ownership and stop-method checks.

This repair does not change ELN callback acknowledgement requirements. A response
without top-level integer code=0 remains a separate delivery failure.
