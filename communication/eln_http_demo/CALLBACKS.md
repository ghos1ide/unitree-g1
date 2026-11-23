# ELN 回调约定

真实机器人入口 8090 保留四种异步回调：

| status | 含义 |
| --- | --- |
| ACCEPTED | 任务已持久化并受理，不代表执行成功 |
| RUNNING | 正在执行，可能多次报告进度 |
| SUCCEEDED | 执行成功，最终结果 |
| FAILED | 拒绝、执行失败、取消或重启中断，最终结果 |

HTTP 响应和异步回调是两条不同的返回通道。受理成功仍立即返回
`code=0,status=ACCEPTED`；参数错误、缺少录制、未初始化、忙碌等仍返回原有
HTTP 4xx 和业务错误码，不伪造成功。能识别有效 `taskId`、`taskType` 和
`callbackUrl` 的新任务拒绝，会额外保存 `FAILED` 结果和回调，并返回
`status=FAILED,callbackScheduled=true`。初始化请求的类型由接口确定。
未知内部错误返回 HTTP 500、业务码 9000，并在同样条件下保存失败回调。

缺少任务标识、缺少或无效的回调地址、无法解析的 JSON，只能在当前 HTTP
响应中说明失败，返回 `callbackScheduled=false`。未通过内部鉴权的请求不触发
外部回调。服务未收到请求、进程尚未持久化便崩溃、数据库或网络不可用时，
不能保证立即返回或送达；ELN 应保存 HTTP 响应并按原 taskId 查询，不盲目重复执行。

拒绝的任务号也会保留。修改请求后重新下发应使用新 taskId。重复 taskId 不会
重新执行，也不会覆盖原结果或发送一个与原任务冲突的 FAILED；HTTP 响应中的
`duplicate=true` 和 `status` 表明沿用原任务状态，可查询原任务及回调投递情况。

回调沿用 `taskId,taskType,status,progress,message,timestamp,errorCode` 字段。
取消时对外状态为 FAILED，`errorCode=CANCELLED`，内部查询仍可显示 CANCELLED。
失败原因保留在 message；受理前失败不会伪造 ACCEPTED 或 RUNNING。
参数错误回调包含字段错误说明。无法识别的 taskType 会按原非空字符串回传。

结果和发送队列在同一 SQLite 事务中保存。SUCCEEDED/FAILED 持续重试，指数退避
最高 60 秒，直到收到 HTTP 2xx 且 JSON 中整数 `code=0`。ACCEPTED/RUNNING
最多尝试 4 次，避免阻塞最终结果。重试使用相同 payload 和 timestamp，不重新
执行机器人动作；ELN 应按 taskId、status、progress、timestamp 去重。

服务重启后继续投递持久化结果；未完成任务转 FAILED，绝不自动重放动作。
升级时未送达的历史最终回调（包括之前重试耗尽的记录）恢复持续重试，已确认
送达的记录不重发。回调地址永久不可用时结果仍保留为待投递，可在 8092 监控。

该策略由真实生产 API 的 `Settings(reliable_callbacks=True)` 启用；默认纯模拟
入口保留旧的五状态、有限重试行为，以兼容原有协议测试。机器人内部安全检查
和执行授权不受回调策略影响。`GET /health` 的 `callbackPolicy` 为
`reliable-four-state` 时，表示新策略已在运行进程生效。
