# ELN V1.5 协议对照

本文件保留升级前的对照记录。V1.5 纯接口升级现已实施，当前行为和扩展错误码以 INTERFACE_ONLY.md 为准。

日期：2026-09-20。依据用户提供的《人形机器人_ELN_HTTP_API协议_V1.5.html》。
对照对象为当前纯接口入口 deployment/interface_service.py 及其复用的 robot_gateway、模拟 ELN 接收端。
本次仅对照和验证，不修改运行代码，不接入机械臂动作，不排查网络。

## 结论

V1.5 存在不兼容变更，当前纯接口服务仍是 V1.0 任务语义，不能直接接收新版任务。
接收、异步执行、回调、查询、取消的整体结构可保留；任务模型、模拟执行计数、错误码、
模拟回调去重和测试示例需要升级。路径仍为 /api/v1，不应因文档版本为 V1.5 而改成 /api/v1.5。

## 明确差异

| 项目 | 当前实现 | V1.5 要求与影响 |
| --- | --- | --- |
| 前轮任务 | PEDAL_ROTATE，direction=LEFT/RIGHT，count | 改为 NWS_CYCLE，仅 count；旧枚举会拒绝新请求 |
| 前轮计数 | 一个方向完成一次计数 | 左旋一次加右旋一次才算一组；count=20 对应 20 组，不是 20 个单向动作 |
| WOW 任务 | MLG_WOW，wowState=ON/OFF | 改为 NLG_WOW_OPERATION，wowSignal=AIR/GROUND；不仅改名，还由主起落架语义改为前起落架目标信号语义 |
| WOW 成功 | 模拟一次操作完成 | 新协议要求目标 WOW 信号达成；纯接口模式只能模拟达成，不可宣称验证了真实信号 |
| 起落架参数 | GEAR_OPERATION，action=RETRACT/EXTEND | 改为 count、preAction、cycleMode；当前校验器要求 action，拒绝三个新字段 |
| 起落架总次数 | 固定为 1 | 应为 count，影响成功判定和持久化状态的进度上限校验 |
| 起落架预动作 | 不支持 | NONE 或 EXTEND_DOWNLOCK；预先放下并确认下位锁，不计数 |
| 起落架完整循环 | 不支持 | FULL_CYCLE：每次收起再放下，确认下位锁后计数 |
| 最终仅收起 | 不支持 | FINAL_RETRACT_ONLY：前 count-1 次完整循环；末次仅收起、确认上位锁后计数，禁止再放下 |
| 初始化未完成 | 返回 2002 | 新增专用错误码 2003；2002 保留为安全条件不满足 |
| 2004 含义 | 任务不存在 | 新版定义为执行机构故障；不能继续用它表示查询目标不存在 |
| 回调去重 | taskId + status + timestamp | 增加 progress，改为四字段；模拟 ELN 的数据库唯一约束需同步修改 |

关键代码位置：robot_gateway/models.py 的 TaskType、PARAMETERS、TaskRequest.total；
robot_gateway/adapters.py 的 LocalExecutor.execute；robot_gateway/manager.py 的 submit、query；
robot_gateway/store.py 的 transition；pc_a_simulator/callback_server.py 的 events 表。

## 保持一致的部分

- 四个业务路径和 POST 方法不变：初始化、任务下发、查询、取消。
- 接收响应仍为 code=0、message=accepted、原始 taskId、status=ACCEPTED。
- 异步回调仍含 taskId、taskType、status、progress、message、timestamp、errorCode。
- 状态仍为 ACCEPTED、RUNNING、SUCCEEDED、FAILED、CANCELLED，终态不可回退。
- progress 仍是次数，不是百分比；失败或取消保留完整完成的次数。
- 时间使用带时区的 ISO 8601。当前使用 UTC 的 +00:00，符合要求，无需强制改为示例中的 +08:00。
- ELN 接收回调后返回 code=0；重试通知不能重新执行任务。
- 任务号去重、单任务执行、状态与待发送回调共同持久化机制可以复用。

## 初始化与错误处理需明确的边界

1. V1.5 初始化示例同时提供 taskId、callbackUrl；通用约定明确回调地址由 ELN 在任务请求中提供。
   当前仍允许生成 taskId 和回退到服务默认回调地址。建议新版严格要求请求提供二者；
   但初始化章节未给单独的必填表，是否保留旧版兼容行为应明确记录。
2. V1.5 的 /tasks 类型列表只有三种控制任务，初始化有独立入口。
   当前 /tasks 也接受 ROBOT_INITIALIZE。严格新版入口应限制为三种控制任务，内部事件类型仍需保留 ROBOT_INITIALIZE。
3. 新版错误码表不再定义“任务不存在”，也未列出旧版 9000 内部故障。
   HTTP 404 可以表示目标不存在，但响应体的 code 仍需双方补充约定，不能擅自复用 2004。
   当前还使用 CONTROL_FAILED、GATEWAY_RESTARTED 等字符串错误码，需要确认 errorCode 的枚举约定。
4. 查询、取消的完整响应 JSON 和 HTTP 错误状态码仍未给出，可保留已有结构并作为补充约定。
5. preAction、cycleMode 没有给出省略时的默认值。建议新模型要求显式提供，避免默认为某种循环语义。
6. 新版允许失败重发，但没有保留“最多重试三次”的文字。当前三次重试可以保留为实现策略，不能再称其为新版明确要求。
7. 鉴权具体方式仍未定义。当前纯接口入口的 X-API-Key 属于实现约定。

## 纯接口模式的升级范围

不实现真实机械控制，但需要模拟新版阶段顺序，避免只替换任务名称造成错误的计数行为。

- NWS_CYCLE：模拟 LEFT、RIGHT 两个阶段，只有一组完成后 progress 才加 1。
- NLG_WOW_OPERATION：模拟 AIR/GROUND 目标达成，完成后 progress=1。
- GEAR_OPERATION：先模拟可选预动作且保持 progress=0，再按循环模式计数。
- count=1 且 FINAL_RETRACT_ONLY 时，正式计数部分只模拟一次收起，不模拟随后放下。
- 失败或取消发生在循环中途时，不把未完整完成的循环计入 progress。
- 状态、错误码、模拟 ELN 的 TaskEvent 校验与去重键、OpenAPI 版本说明和调用示例一同升级。
- 原有 ROS2 和硬件接口不在本次纯接口范围。共享模型变更时需要隔离或明确旧入口兼容性，不能默默改变其他演示流程。
- 旧数据库含旧类型任务；Store.transition 会用当前模型重新解析历史请求。
  因此升级需兼容解析历史数据或使用独立新版数据库，不能直接换枚举后复用所有旧数据，更不能删除历史去重记录。

## 已执行的验证

使用 Unitree Python 环境直接调用当前 TaskRequest 校验器，未发送 HTTP 任务：

| V1.5 示例 | 当前结果 |
| --- | --- |
| NWS_CYCLE，count=20 | 拒绝：taskType 枚举不匹配 |
| NLG_WOW_OPERATION，wowSignal=AIR | 拒绝：taskType 枚举不匹配 |
| GEAR_OPERATION，count=5，preAction=NONE，cycleMode=FULL_CYCLE | 拒绝：缺少 action，三个新字段不允许 |

未改运行代码，未重新编译；未读取硬件、未执行运动、未做网络测试。

## 升级验收建议

至少覆盖前轮完整组与半组取消、WOW 两种信号、起落架两个 cycleMode 和两个 preAction 的组合、
count=1 边界、预动作失败或取消不计数、末次收起后不再放下、2003 初始化门禁、
重复任务拒绝、新回调去重键、持久化兼容，以及新版真实 HTTP 请求到模拟 ELN 的回调闭环。
