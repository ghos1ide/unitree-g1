# Postman 下发其他任务

这里的“模拟”是用 Postman 模拟 ELN 客户端。8090 连接真实机器人，Send 会触发真实任务，不是离线仿真。
不要使用 Run Collection 批量运行下面的动作。本文和集合没有发送任何请求。

## 设置

1. Postman → Import → 选择同目录 `postman_production_tasks.json`。
2. 在集合 Variables 中设置 `baseUrl` 为你初始化请求使用的机器人地址，例如 `http://192.168.0.249:8090`，以实际可达地址为准。
3. 设置 `callbackUrl` 为机器人能访问的实际回调服务地址。不能填写电脑的 localhost/127.0.0.1，那会指向机器人自己。Postman 本身不是回调接收服务器。
4. 当前生产 8090 配置无需 X-API-Key。请求使用 Body → raw → JSON，Content-Type 为 application/json。
5. 先发送 GET `/health`。动作要求初始化已成功、没有活动任务、无需恢复且控制权有效。上一轮检查曾处于 requiresRecovery=true，需重新确认现场状态，不能直接假定现在就绪。
6. 运动前确认进程所有权、手部映射与串口访问、作业空间、机器人稳定和可立即使用的物理急停。仅在这些前提满足时单独发送目标动作。

回调接收端成功处理后应返回 HTTP 2xx 和 `{"code":0}`。最终回调会持续重试，不要随意填一个不存在的地址。
没有回调接收端时仍可用查询接口了解已提交任务，但这不构成完整回调联调。

## NWS 一次循环

POST `{{baseUrl}}/api/v1/tasks`

```json
{
  "taskId": "PM-NWS-001",
  "taskType": "NWS_CYCLE",
  "callbackUrl": "{{callbackUrl}}",
  "parameters": {"count": 1}
}
```

当前绑定 dring_pair，一次完整动作才计一次。count 必须是正整数，不能写字符串 "1"。

## AIR / GROUND

POST `{{baseUrl}}/api/v1/tasks`

```json
{
  "taskId": "PM-AIR-001",
  "taskType": "NLG_WOW_OPERATION",
  "callbackUrl": "{{callbackUrl}}",
  "parameters": {"wowSignal": "AIR"}
}
```

当前 AIR 对应 push。下发 GROUND 时改为 `"wowSignal":"GROUND"` 并换新的 taskId，当前对应 pull。
SUCCEEDED 只表示动作序列完成，不代表已测得外部 WOW 信号。

## 直接选择完整录制动作

POST `{{baseUrl}}/api/v1/actions/{{action}}/tasks`

```json
{
  "taskId": "PM-ACTION-001",
  "callbackUrl": "{{callbackUrl}}"
}
```

集合变量 action 可选择当前包中的：

- `dring_pair`
- `push`
- `pull`
- `gear_roundtrip_lower_upper`
- `gear_roundtrip_upper_lower`

此接口不要额外添加 taskType 或 parameters。当前生产 `GEAR_OPERATION` 组合业务未配置，会被拒绝；录制动作接口不等于组合业务已实现。

## 查询与取消

查询：POST `{{baseUrl}}/api/v1/tasks/{{taskId}}/query`，Body `{}`。

取消：POST `{{baseUrl}}/api/v1/tasks/{{taskId}}/cancel`，Body：

```json
{"reason":"Postman operator cancelled"}
```

受理响应中的 `code=0,status=ACCEPTED` 只说明已接收。后续查询或回调确定 RUNNING、SUCCEEDED、FAILED 等状态。
取消是受控停止请求，不是物理急停，也不代表已经确认停止；继续查询结果。

集合的动作请求每次 Send 自动生成新 taskId 并保存在集合变量中，查询/取消默认针对最近一次提交。
不要创建同名环境变量覆盖它。需要查询旧任务时，将集合 taskId 改为原值。
如果提交超时或响应不明确，先查询原 taskId，不能再次点击动作 Send；再次 Send 会生成一个全新的任务。
手动使用本文 JSON 时，每个新任务自己换唯一 taskId；已被拒绝的任务号也可能已持久化。

常见业务码：1001 参数/动作不支持，1003 重复任务号，2001 忙碌，2003 初始化或恢复前置条件未满足。
具体原因以响应 message 为准。此集合不包含自动初始化、自动恢复或自动重试动作。
