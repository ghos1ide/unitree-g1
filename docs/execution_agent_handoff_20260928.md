# 通信现状与执行开发交接

核对日期：2026-09-28。项目根目录：`/home/unitree/rh56dfx/workspace`。
本文是代码、配置和只读在线检查的快照，不是整机验收报告。

## 1. 当前实现程度

通信已从模拟接口推进到真实执行后端接入，可以作为执行开发的现有入口继续使用。
不能据此宣称 ELN 所有业务闭环或全部真实动作已验收。

| 能力 | 当前情况 |
| --- | --- |
| 对外 HTTP | 8090 为真实转发入口，提供业务接口和 `/docs`，转发到本机 8795 |
| 生产执行 API | `production_api.py` 使用 `recorded-native` 后端；8794 是内部 worker IPC |
| 初始化与动作提交 | 异步受理；HTTP ACCEPTED 不等于动作完成 |
| 查询、取消 | 已实现；取消结果必须区分已确认停止与停止未确认 |
| 状态持久化 | SQLite 保存任务和回调 outbox，任务号去重，重启不自动重放动作 |
| 可靠回调 | 生产模式对外 ACCEPTED、RUNNING、SUCCEEDED、FAILED 四状态 |
| 回调重试 | 最终结果持续重试，退避上限 60 秒；非最终状态最多尝试 4 次；不重放动作 |
| 回调确认 | HTTP 2xx 且 JSON 整数 `code=0`；按 taskId/status/progress/timestamp 去重 |
| 拒绝回调 | 有足够有效任务信息时保存 FAILED 回调；无有效标识/回调地址等情况只能返回 HTTP 错误 |
| 鉴权 | 当前部署约定 8090 不要求客户端密钥；转发到 8795 仍使用内部密钥 |
| 服务管理 | `eln_interface.service`、`eln_executor.service` 在线；被动启动，不自动初始化 |
| 业务完成语义 | `motion-sequence-only`，不代表外部 WOW、上锁、下锁信号已验证 |

生产回调契约以 `communication/eln_http_demo/CALLBACKS.md` 为准。
取消对外回调为 FAILED、`errorCode=CANCELLED`，内部查询可以保留 CANCELLED。
重复 taskId 不重新执行，不覆盖原任务结果；请求结果不确定时先查原 taskId。
转发失败不回退模拟成功，也不自动重试不确定的动作 POST。

## 2. 当前可用业务与缺口

当前选中版本：`production-f2bfad675dd378ad329c`。
动作包 SHA256：`ee6e0d2d57b4226fce0d39dc1202af7a2257edac0621959f4cfd063dbe27c077`。
动作包路径：`execution/action_runtime_v3/config/selected_bundle.json`。

| 业务 | 当前绑定/限制 |
| --- | --- |
| ROBOT_INITIALIZE | 双臂同时准备，速度/加速度比例 0.7/0.7 |
| NWS_CYCLE | 当前采用 `dring_pair`，完整执行一次才计一次 |
| NLG_WOW_OPERATION | AIR → push，GROUND → pull；只确认动作序列完成 |
| 指定完整录制动作 | `/api/v1/actions/{action}/tasks`，要求选中包内存在动作 |
| 当前录制动作 | dring_pair、push、pull、gear_roundtrip_lower_upper、gear_roundtrip_upper_lower |
| GEAR_OPERATION | 生产入口明确拒绝：组合业务流程尚未配置 |
| GEAR_SEGMENT | 执行代码中存在内部概念，云端字段尚未约定；不能当作已交付公共协议 |

重点缺口：外部业务信号确认、组合起落架流程、真实动作的逐项验收记录、响应时间的实测分解。
有录制、有执行代码、有测试脚本，都不等于该动作已通过现场验收。

## 3. 在线状态快照

本次只读检查中，8090 和 8795 的 `/health` 均返回真实后端状态；两个 systemd 服务为 active。

```json
{
  "backend": "recorded-native",
  "simulation": false,
  "hardwareConnected": true,
  "initialized": false,
  "controllerRunning": false,
  "activeTaskId": null,
  "requiresRecovery": true,
  "completionSemantics": "motion-sequence-only",
  "callbackPolicy": "reliable-four-state"
}
```

服务在线不代表执行就绪。诊断当时还报告控制器/诊断发布者数量不符合预期，以及等待新的 R2+A。
未进一步判断该提示在当前 DETACHED/恢复状态下的具体原因，不能据此直接认定存在竞争控制器。
交接后重新读取状态，不自动清除恢复状态或重启服务来“修复”。

## 4. 执行 agent 的代码入口与边界

路径均相对于项目根目录。

| 层 | 文件/目录 | 责任 |
| --- | --- | --- |
| 外部转发 | `execution/action_runtime_v3/deployment/rail_jetson/http_relay.py` | 8090 转发和错误保持 |
| 公共通信层 | `communication/eln_http_demo/robot_gateway/` | 模型、任务管理、SQLite、回调 |
| 生产 API | `execution/action_runtime_v3/deployment/rail_jetson/production_api.py` | 受理条件、真实模式、stop/recover/release |
| 业务映射 | 同目录 `cloud_executor.py` | 任务到录制动作、计数、版本绑定、审计 |
| 内部 IPC | 同目录 `native_runtime.py`、`native_worker.py` | job、心跳、取消、恢复和执行结果 |
| 动作执行 | 同目录 `executor.py` | SequenceExecutor，join/recorded/hand 步骤 |
| 机器人传输 | 同目录 `ros_transport.py` | 控制权、静止判断、参数切换、规划与轨迹发送 |
| 底层控制 | `execution/action_runtime_v3/native_ws/src/g1_unitree_control/` | 原生 gateway、接管、轨迹、安全停止 |
| 手部契约 | `docs/hand_service_api.md`、相关 `scripts/task_*hand*.py` | 通过 HandClient 使用 hand_service |
| 启动管理 | 同目录 `standalone_supervisor.py`、`run_standalone.sh`；`deploy/` | 被动启动和子进程生命周期 |

执行开发优先限定在 v3 执行目录及必要的共享脚本。修改共享脚本前确认其他入口的调用关系。
保持公共请求字段、错误码、回调格式、taskId 去重、进度计数和重启不重放约定。
需要改变业务完成语义或增加外部信号时，先明确接口契约和验收条件，再同步通信层。
不得另起一套 HTTP/任务存储/串口控制路径来绕过现有实现。

## 5. 必须一起交接的材料

1. **完整代码快照**：当前 `communication/`、`execution/`、`deploy/` 等目录在 Git 中仍为未跟踪，另有已有修改。仅给 commit、分支或普通 `git diff` 不足以复现。整理可审阅的代码快照，保留用户修改，不把大型模型、日志、密钥无差别提交。
2. **版本与动作资源**：选中动作包、版本/SHA、AIR/GROUND 映射、初始化目标、规划参数、URDF/模型及其引用路径。不得只交动作名。
3. **部署配置**：`config/native.yaml`、`config/interface_relay.json`、启动脚本、服务定义和实际生效的 override/env。只交密钥位置与安全获取方式，不在文档、聊天或 Git 中公开 `api.key`、`native.token`。
4. **运行环境**：生产 API 使用 `communication/eln_http_demo/.venv/bin/python`；公开入口配置使用现有 Unitree 环境；native worker 使用 ROS2 Foxy 启动环境。不要擅自统一解释器、重装系统依赖或更改全局 DDS 设置。
5. **状态与持久化**：`execution/action_runtime_v3/data/production.sqlite3`、对应执行审计库、worker journal/恢复标记的路径与用途。保留历史任务号、未送达回调和恢复证据；需要备份在线 SQLite 时使用一致性备份，不只复制主文件而遗漏 WAL。
6. **现场安全信息**：当前控制权、是否有 WSL/其他控制器、机器人支撑/稳定情况、停止手段、允许测试哪只手、最近成功和失败动作。
7. **验收证据**：每个动作的 taskId、动作包 SHA、受理/下发/完成时间、回调记录、日志、停止确认和外部信号证据。没有的部分标记待验证。
8. **目标优先级**：先解决哪种动作、是否要求一秒内开始运动、是否要增加外部信号闭环；不能把“优化执行”解释成删除安全检查。

旧的 `scripts/eln_service.py`、v2、incoming 副本和模拟器 README 不能替代当前生产入口。
部分旧文档仍写“所有执行器都是模拟”，与本次在线真实后端不一致。优先依据当前进程、v3 代码、配置、CALLBACKS.md 和 deploy/eln_executor.md。

## 6. 延迟问题的既有结论

冷初始化：控制权观察 5 秒、就绪后力矩采样 2 秒、接管姿态保持 2 秒、前馈过渡 2 秒，另加启动/规划时间。
这些只在对应冷启动/接管路径出现，不是每个普通动作都执行。

普通手臂段：目前在段开始、set_stage、align_check 分别累计至少 150 ms 静止窗口，总计至少 450 ms 加轮询开销。
另有整包 deepcopy/validate、审计、手部交互、连接段规划、轨迹转换/传输等成本。尚未做分段实测。

一秒目标应定义为：已初始化、控制权持续有效、反馈新鲜且静止时，从 POST 收到到首段实际开始运动。
记录受理、worker 开始、规划完成、轨迹下发、控制器接受、实际运动时间；报告分位数与失败条件。
可研究固定包预校验、持续静止证据、减少重复计算。状态变化后重新确认，保留下发前的实时安全检查。
本轮讨论尚未实施上述优化，也未承诺当前系统满足一秒时限。

## 7. 验证与安全要求

本次完成：读代码/配置、Git 状态、服务状态、HTTP GET 健康检查。
编译：未执行。自动化测试：本次未重跑。硬件读取：仅查看既有服务聚合的反馈/诊断，未直接访问硬件。
动作测试：未执行；未初始化、恢复、停止、重启或启动任何硬件服务。

已有离线验证入口包括：

- `communication/eln_http_demo/tests/test_reliable_callbacks.py`：可靠回调与任务状态。
- `communication/eln_http_demo/tests/test_interface_http.py`：模拟 HTTP 联调。
- `execution/action_runtime_v3/deployment/rail_jetson/test_http_relay.py`：MockTransport 转发测试。
- 同目录 `HEADLESS_TESTING.md`、`STANDALONE_TESTING.md`：更大范围验证说明。执行前逐项辨别离线与硬件步骤，不整篇直接运行。

实际代码修改后遵循根目录 AGENTS.md 的编译/CTest 要求，并运行受影响模块的离线测试；分开报告编译、硬件读取与动作实测。
全局环境修改、仓库外写入先询问用户。SDK 参考目录只读。
hand_service 是唯一串口硬件所有者，手部控制只用 HandClient。
手部映射：右 FTDI if01、左 if02，均 ID 1；默认使用稳定 by-id 路径。
q=0 闭合、q=1 张开，raw=q*1000；每手顺序 pinky/ring/middle/index/thumb_bend/thumb_rotation，先右后左。
执行器反馈不等于外部指节角度测量。
任何动作前先明确警告用户，验证映射、串口访问、进程所有权、作业空间、机器人稳定和立即停止手段。
严格遵守单手测试范围。不得以诊断为由启动 hand_service。
优雅退出或软件停止不能代替物理急停/断电；不要强杀承担支撑的控制进程。

## 8. 可直接发送给执行 agent 的任务说明

请先阅读根目录 AGENTS.md 和本交接文档，基于当前完整工作区而非仅 Git HEAD 开展执行开发。
以 action_runtime_v3 为生产实现，沿用 8090 → 8795 → native worker → gateway 的调用链和 HandClient 手部接口。
先用只读方式确认版本、运行进程、控制权与恢复状态，再定位用户指定的执行问题。
保持现有 ELN 请求/回调、幂等与完成语义，先补分段耗时证据，再优化普通动作响应。
不自动恢复、初始化、重放任务或重启控制服务；任何硬件动作必须满足项目安全规则。
交付代码差异、离线验证结果、未覆盖的硬件风险及清晰的现场验收步骤。
