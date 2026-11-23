# ELN Robot Communication Simulator V1.5

中文人工调试指南：[接口文档使用手册](DOCS_USER_MANUAL.md)。

机器人只读可视化监控（8092）：[ELN 联调监控使用说明](MONITOR.md)。

Implements the supplied ELN HTTP API V1.5. Local development uses Conda `unitree`
(Python 3.11). The robot uses its existing Conda `Unitree` (Python 3.8.20) with
ROS2 Foxy. All delivered executors are simulations; no physical controller is connected.

## Run

- Double-click `start_demo.bat` for the local Chinese desktop simulator.
- Double-click `start_robot_demo.bat` after upgrading the robot to V1.5.
- Click the full-example button to run initialization, NWS, both WOW targets,
  full gear cycles, final-retract-only cycles and a pre-extension example.
- Select an individual task to change count, WOW signal, pre-action or cycle mode.
  Initialization runs automatically when needed.
- Select a simulation fault to test failure behavior. The occurrence field selects
  the matching stage within each task: e.g. DOWNLOCK_TIMEOUT occurrence 2 fails
  the second downlock confirmation, including a pre-action confirmation if present.
- Cancel stops at the next simulated stage boundary. Only completed operations count.

The window shows actual HTTP callbacks, current stage, completed count and failure
code. Closing it stops its own callback service and tunnel. A remote gateway remains
running. Gateway and ELN log data are saved in `data/desktop_demo/<session>/`.

## V1.5 Contract

All business endpoints use POST. Initialization is submitted only through
`/api/v1/robot/initialize`, with required `taskId` and `callbackUrl`:

```json
{"taskId":"INIT-001","callbackUrl":"http://127.0.0.1:8001/eln/api/v1/robot-callbacks/task-status"}
```

Control tasks use `/api/v1/tasks`:

```json
{
  "taskId": "NWS-001",
  "taskType": "NWS_CYCLE",
  "callbackUrl": "http://127.0.0.1:8001/eln/api/v1/robot-callbacks/task-status",
  "parameters": {"count": 20}
}
```

| Task | Required parameters | Completion/count condition |
| --- | --- | --- |
| NWS_CYCLE | count | LEFT followed by RIGHT is one operation |
| NLG_WOW_OPERATION | wowSignal: AIR or GROUND | Target signal confirmed, progress=1 |
| GEAR_OPERATION | count, preAction, cycleMode | Mode-specific lock confirmation |

`preAction`: NONE or EXTEND_DOWNLOCK. Pre-extension must confirm downlock and never
increments progress. `cycleMode`: FULL_CYCLE or FINAL_RETRACT_ONLY. FULL_CYCLE counts
after retract/uplock/extend/downlock. FINAL_RETRACT_ONLY performs count-1 full cycles,
then retract/uplock only, counts the last operation, and does not extend again.
For count=1 this means a single retract and uplock confirmation.

| Endpoint | Purpose |
| --- | --- |
| POST /api/v1/robot/initialize | Initialize |
| POST /api/v1/tasks | Submit a control task |
| POST /api/v1/tasks/{taskId}/query | Current status and completed operations |
| POST /api/v1/tasks/{taskId}/cancel | Controlled stop; body: {"reason":"operator cancelled"} |
| GET /health | Backend, simulation flag and protocolVersion |
| POST /api/v1/simulation/config | Simulator-only extension, not part of ELN V1.5 |

Errors: 1001 parameters, 1002 unauthorized, 1003 duplicate taskId, 2001 busy,
2002 safety interlock, 2003 initialization incomplete, 2004 actuator/confirmation
failure. FAILED callbacks encode the business error code as a string.
V1.5 does not specify an unknown-task code; this implementation uses HTTP 404 plus
1001 and `task not found`. Transport exceptions and interrupted tasks use 2004 with
an explicit message; they do not imply confirmed physical stopping or completion.

## Execution and Persistence

One task executes at a time. taskId remains unique across restarts. Terminal states
are immutable. Failures and process restarts clear the initialization gate.
Commands and sensor confirmations are separate adapter methods. The executor checks
cancellation and safety before every stage and counts only complete operations.
RUNNING callbacks also identify stages while progress stays unchanged.

SQLite commits state and callback outbox together. Callbacks retry the identical
payload three times after the first attempt. Failure to deliver never repeats an
action. Success requires HTTP 2xx and integer JSON code=0. Delivery exhaustion is
visible in query.callbackDelivery. Callback deduplication uses exactly
taskId + status + progress + timestamp, as specified in V1.5.

Old PEDAL_ROTATE, MLG_WOW and gear action requests are rejected. Stored V1.0 records
are retained and can be queried; interrupted historical tasks are failed without
replaying them. Old callback receiver tables are copied into the V1.5 schema without
deleting the original table. RobotTask.action is wire-incompatible with the old
definition: rebuild and restart both ROS nodes together.

Simulation faults: NONE, SAFETY_INTERLOCK, WOW_TIMEOUT, UPLOCK_TIMEOUT,
DOWNLOCK_TIMEOUT, ACTUATOR_FAULT. Set with
`{"fault":"DOWNLOCK_TIMEOUT","occurrence":2}`. Configuration is rejected while busy
and is disabled for non-simulation backends. The fake Action goal carries these
settings internally; they are not added to the ELN task parameters.

## Manual Local Commands

In separate terminals at the project root, activate `conda activate unitree`:

```powershell
python -m uvicorn pc_a_simulator.callback_server:app --host 127.0.0.1 --port 8001
python -m uvicorn robot_gateway.app:app --host 127.0.0.1 --port 8000 --workers 1
python -m pc_a_simulator.eln_simulator --count 3
```

Swagger: http://127.0.0.1:8000/docs. Do not use multiple workers or hot reload while
tasks execute. Dependencies: `python -m pip install -r requirements.txt`.

## Robot Deployment

Target: `unitree@192.168.0.249`.
Project: `/home/unitree/rh56dfx/workspace/communication/eln_http_demo`.
Interpreter: the local `.venv/bin/python`; see the [environment setup](../../docs/python_environment.md).
No new environment is needed. Use `python -m deployment.deploy_robot_demo` from the
local Conda unitree environment to upload and upgrade the existing robot project.
It refuses to stop an active task, backs up the deployment, rebuilds Action interfaces,
tests the core, and restarts the two demo services. A build/test failure leaves the
services stopped and preserves the backup for diagnosis.

Manual startup on the robot, in two SSH terminals at the deployed project root:

```bash
bash deployment/start_fake_controller.sh
bash deployment/start_ros2_gateway.sh
```

The scripts source Foxy and the built interfaces, select domain 73 and localhost-only
DDS, and use `/eln_demo/robot_task`. The gateway binds port 18000 and requires the
API key in `data/robot_connection.json`. Do not disclose that file. The desktop
callback tunnel uses robot loopback port 18081; verification uses 18082. Open one
robot desktop window at a time. The desktop rejects a pre-V1.5 remote gateway.

Robot-side observation:

```bash
tail -f data/server.log
tail -f data/controller.log
source deployment/ros2_env.sh
ros2 topic echo /eln_demo/robot_task/_action/feedback
```

Controller logs include accepted parameters, each stage, feedback, cancellation and
the result. Current deployment layout has two ROS processes on the same robot;
cross-host DDS and physical control are separate integration steps.

## Verification

```powershell
python -m pytest -q
python -m deployment.verify_robot_demo
```

The first command tests the local interface, execution sequences, full-cycle counts,
lock/WOW failures, cancellation boundaries, retries, persistence and desktop controls.
The second tests an upgraded robot through actual HTTP, ROS2 and SSH callbacks.
Local tests do not establish remote deployment success.
