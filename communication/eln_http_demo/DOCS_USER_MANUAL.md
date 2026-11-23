# ELN V1.5 接口文档使用手册

适用项目：机器人 PC2 上的 ELN 通信服务。核对日期：2026-09-23。

本手册面向通过浏览器人工调试接口的用户，以当前代码为准。
使用 `deployment.interface_service` 启动的是纯模拟服务，不连接机械臂、灵巧手、ROS2 或真实传感器。
模拟成功只代表接口流程完成，不代表实际机械动作已完成。

## 1. 先理解三个角色

| 角色 | 功能 |
| --- | --- |
| 机器人接口服务 | 在 PC2 上运行，接收任务、模拟执行、保存结果、发送回调 |
| 机器人 `/docs` 页面 | 供人查看接口和发送测试请求；点击 Execute 相当于代替 ELN 发请求 |
| ELN 回调接收端 | 接收机器人主动推送的状态；真实 ELN 未部署时用模拟接收端代替 |

```text
你的浏览器 /docs ──POST 任务──> 机器人服务 :8090
               <──ACCEPTED────
                                  │ 后台模拟执行
                                  └──POST 状态──> ELN 回调接收端
                                                 └──返回 code=0
```

正式 ELN 程序直接调用业务接口，不必打开或经过 `/docs`。
关闭浏览器页面不会取消任务或停止机器人服务。

## 2. 访问地址怎么选

| 访问位置 | 机器人文档地址 |
| --- | --- |
| 机器人本机浏览器 | http://127.0.0.1:8090/docs |
| 与机器人 Wi-Fi 网络可互通的电脑 | http://192.168.0.249:8090/docs |
| 能访问机器人有线网段的电脑 | http://192.168.123.164:8090/docs |
| 已打通 5G 端口转发的 ELN 所在网络 | http://172.18.200.22:8090/docs |

这些地址是否可用取决于服务是否启动及实际网络配置；Wi-Fi 地址可能变化。
5G 地址可用还需转发到 PC2 的 TCP 8090，不能仅因本机打开成功就认为远端已连通。
当前服务提供 HTTP，不要自行把前缀改为 HTTPS。

`http://192.168.123.164:8090/api/v1/tasks` 中：IP 是目标电脑地址，8090 是服务端口，
`/api/v1/tasks` 是协议固定的任务接口路径。`taskType` 才决定要做哪类任务。
文档版本是 V1.5，但接口路径仍然是 `/api/v1`。

## 3. 启动准备

生产部署使用 systemd 开机自启。若服务已在运行，直接进入第 4 节，不要在同一端口重复启动。

### 3.1 检查机器人服务

```bash
systemctl status eln_interface.service --no-pager
curl --noproxy '*' -fsS http://127.0.0.1:8090/health
```

服务监听 `0.0.0.0:8090`，表示接受各网卡的请求；浏览器应填写上表中的具体地址。
systemd 会在机器人开机后启动服务，并在进程退出后自动拉起。若尚未安装该服务，按
[纯接口联调说明](INTERFACE_ONLY.md)中的“开机自启”步骤安装。安装会写入系统目录，
应由机器人管理员执行。

仅在调试且 systemd 服务已停止时，才使用前台启动命令：

```bash
cd /home/unitree/rh56dfx/workspace/communication/eln_http_demo
/home/unitree/.miniconda3/envs/Unitree/bin/python -m deployment.interface_service --host 0.0.0.0 --port 8090
```

### 3.2 启动模拟 ELN 回调接收端

另开一个机器人终端：

```bash
cd /home/unitree/rh56dfx/workspace/communication/eln_http_demo
/home/unitree/.miniconda3/envs/Unitree/bin/python -m uvicorn pc_a_simulator.callback_server:app --host 0.0.0.0 --port 8091
```

此处监听所有网卡是为了允许你的电脑打开模拟 ELN 文档。
它只用于测试，不要把模拟接收端当成正式 ELN 服务。
在机器人浏览器可打开 http://127.0.0.1:8091/docs；在同一 Wi-Fi 的电脑可打开 http://192.168.0.249:8091/docs。

本手册统一使用以下测试回调地址：

```text
http://127.0.0.1:8091/eln/api/v1/robot-callbacks/task-status
```

回调由机器人服务发出，因此这里的 `127.0.0.1` 指机器人自身，不是操作浏览器的电脑。
若回调接收端在其他电脑上，必须换成那台电脑的实际可达地址。

### 3.3 确认服务类型

在机器人文档中展开 `GET /health`，点击 Try it out，再点击 Execute。
应看到 `status=ok`、`protocolVersion=1.5`、`backend=mock`、`simulation=true`。
刚启动时 `initialized=false`，尚无任务时 `activeTaskId=null`。
如果不是 mock/simulation=true，不按本手册发送测试任务，先确认启动的是纯模拟入口。

## 4. 文档页面的通用操作

1. 找到要调用的接口，点击接口所在行展开。
2. 点击 **Try it out**，开启编辑。
3. 若有 `task_id` 参数，在单独输入框中填写实际任务号，不保留花括号。
4. 若有 **Request body**，用本手册对应的完整 JSON 替换页面默认内容。
5. 点击 **Execute**，只发送一次请求。
6. 查看下方 **Server response** 中的状态码和 **Response body**。

页面的 **Responses** 区域可能只是格式说明，不能当作实际调用结果。
查询不会自动刷新，再点击 Execute 才会取得新状态。
查询接口没有请求体，只填任务号即可。

若部署时设置了 `ROBOT_API_KEY`，业务接口中会出现 `x-api-key` 请求头输入框，填入部署方给的值。
没有设置密钥时可留空。当前接口使用 X-API-Key，不使用早期脚本的 Bearer Token。
密钥不要填入 JSON、callbackUrl 或 taskId。

## 5. 第一次完整测试：初始化、下发、查询、查看回调

下面的任务号仅为示例。首次使用后，下次测试请换一个编号，例如末尾改为 002。
任务号保存在数据库中，重启服务不会释放已用过的编号。

### 5.1 初始化

在机器人文档选择 `POST /api/v1/robot/initialize`，填写：

```json
{
  "taskId": "INIT-MANUAL-001",
  "callbackUrl": "http://127.0.0.1:8091/eln/api/v1/robot-callbacks/task-status"
}
```

这两个字段均必填。正常接收响应示例：

```json
{
  "code": 0,
  "message": "accepted",
  "taskId": "INIT-MANUAL-001",
  "status": "ACCEPTED"
}
```

ACCEPTED 仅表示已接收。使用 `POST /api/v1/tasks/{task_id}/query`，
task_id 填 `INIT-MANUAL-001`，等待返回 `SUCCEEDED`、`progress=1` 后继续。
若初始化失败，先排查并重新使用新任务号初始化。

### 5.2 提交前轮转弯任务

在机器人文档选择 `POST /api/v1/tasks`，填写：

```json
{
  "taskId": "NWS-MANUAL-001",
  "taskType": "NWS_CYCLE",
  "callbackUrl": "http://127.0.0.1:8091/eln/api/v1/robot-callbacks/task-status",
  "parameters": {"count": 3}
}
```

表示模拟三组“左旋一次 + 右旋一次”，不是三个单向动作。不需要 direction 字段。
默认每个模拟动作或信号确认阶段约 0.5 秒，这项任务约 3 秒，实际 HTTP 回调到达时间可能更晚。

### 5.3 查询任务结果

在查询接口中将 task_id 改为 `NWS-MANUAL-001`，点击 Execute。
执行中为 RUNNING，最终应为 SUCCEEDED、progress=3。

查询结果的重要字段：

| 字段 | 含义 |
| --- | --- |
| code | 查询本身是否成功；code=0 不代表任务一定 SUCCEEDED |
| status | 当前任务状态 |
| progress | 已完整完成的操作次数，不是百分比 |
| message | 当前模拟阶段或结果说明 |
| errorCode | 非失败状态为 null，FAILED 时为字符串业务错误码 |
| cancelRequested | 是否曾请求取消此任务 |
| callbackDelivery.delivered | 已确认投递的回调事件数 |
| callbackDelivery.pending | 尚在等待投递或重试的回调事件数 |
| callbackDelivery.exhausted | 已耗尽重试次数的回调事件数 |

回调事件数量不是动作次数；子阶段也会产生事件，因此 delivered 可以大于 progress。

### 5.4 查看模拟 ELN 收到的回调

切换到 **8091 端口的模拟 ELN 文档**，选择
`POST /eln/api/v1/tasks/{task_id}/events`，填写 `NWS-MANUAL-001` 后 Execute。

应在 events 列表中看到 ACCEPTED、RUNNING 和 SUCCEEDED。
回调示例（时间和 message 仅作说明）：

```json
{
  "taskId": "NWS-MANUAL-001",
  "taskType": "NWS_CYCLE",
  "status": "RUNNING",
  "progress": 1,
  "message": "Completed 1/3",
  "timestamp": "2026-09-23T09:00:00+00:00",
  "errorCode": null
}
```

多个 RUNNING 可能具有相同 progress，但 message 表示不同阶段，这是正常行为。
不要要求回调条数恰好等于 count，也不要只用 taskId/status 去重。
ELN 应按 taskId + status + progress + timestamp 去重，保存后回复 HTTP 2xx 和 JSON `code=0`。

只有“任务查询成功”和“模拟 ELN 实际收到最终回调”都确认，才算完成这次双向接口测试。

## 6. 其他任务的完整示例

都提交到机器人 `POST /api/v1/tasks`，一次执行一个任务；上一个任务结束后再提交下一个。

### 6.1 WOW 空中状态

```json
{
  "taskId": "WOW-AIR-001",
  "taskType": "NLG_WOW_OPERATION",
  "callbackUrl": "http://127.0.0.1:8091/eln/api/v1/robot-callbacks/task-status",
  "parameters": {"wowSignal": "AIR"}
}
```

模拟设置 AIR 并确认目标状态后，SUCCEEDED、progress=1。
测试地面状态时将 wowSignal 改为 GROUND，并使用新任务号 `WOW-GROUND-001`。

### 6.2 起落架完整循环

```json
{
  "taskId": "GEAR-FULL-001",
  "taskType": "GEAR_OPERATION",
  "callbackUrl": "http://127.0.0.1:8091/eln/api/v1/robot-callbacks/task-status",
  "parameters": {"count": 2, "preAction": "NONE", "cycleMode": "FULL_CYCLE"}
}
```

模拟两次“收起、确认上位锁、放下、确认下位锁”，最终 progress=2，模拟最终锁状态 DOWN。

### 6.3 起落架最后仅收起

```json
{
  "taskId": "GEAR-FINAL-001",
  "taskType": "GEAR_OPERATION",
  "callbackUrl": "http://127.0.0.1:8091/eln/api/v1/robot-callbacks/task-status",
  "parameters": {"count": 2, "preAction": "NONE", "cycleMode": "FINAL_RETRACT_ONLY"}
}
```

第一次数为完整收放循环，最后一次只收起并确认上位锁，不继续放下。
最终 progress=2，模拟最终锁状态 UP。count=1 时，正式计数部分只执行收起和上位锁确认。

### 6.4 正式计数前先放下

```json
{
  "taskId": "GEAR-PRE-001",
  "taskType": "GEAR_OPERATION",
  "callbackUrl": "http://127.0.0.1:8091/eln/api/v1/robot-callbacks/task-status",
  "parameters": {"count": 2, "preAction": "EXTEND_DOWNLOCK", "cycleMode": "FULL_CYCLE"}
}
```

先模拟放下并确认下位锁，此时 progress 仍为 0；随后执行两次完整循环，最终 progress=2。
预动作不会使最终进度变为 3。

## 7. 测试取消

1. 先确认初始化成功、当前没有其他任务。
2. 按第 5.2 节提交 NWS_CYCLE，任务号换成 `NWS-CANCEL-001`，count 改成 20。
3. 立即展开机器人 `POST /api/v1/tasks/{task_id}/cancel`。
4. task_id 填 `NWS-CANCEL-001`，请求体填下面内容，然后 Execute。

```json
{"reason": "manual test cancellation"}
```

取消响应中的 `cancellation requested` 表示请求已收到，不表示已停止。
当前模拟执行器在阶段边界检查取消，查询并等待最终 CANCELLED；回调也会上报 CANCELLED。
只计完整操作：前轮只完成左旋时不计数，起落架完整循环只完成收起时也不计数。
取消与最后一步完成接近时，progress 可能已经等于总次数；以最终 status 为准。
已终结任务再次取消返回 already finished，不改变其最终状态。
HTTP 取消不是物理急停。

## 8. 模拟故障测试（可选扩展）

`POST /api/v1/simulation/config` 是本项目的模拟扩展，不是 ELN V1.5 标准业务接口。
它改变模拟器的故障配置，不控制真实硬件。必须在没有任务运行时配置，否则返回 2001。

先初始化成功，再配置 WOW 确认失败：

```json
{"fault": "WOW_TIMEOUT", "occurrence": 1}
```

然后使用新任务号提交 NLG_WOW_OPERATION。预期最终 FAILED、errorCode="2004"、progress=0。

| fault | 模拟情况 |
| --- | --- |
| NONE | 无故障 |
| SAFETY_INTERLOCK | 安全检查不通过，错误码 2002 |
| WOW_TIMEOUT | WOW 目标确认失败，错误码 2004 |
| UPLOCK_TIMEOUT | 上位锁确认失败，错误码 2004 |
| DOWNLOCK_TIMEOUT | 下位锁确认失败，错误码 2004 |
| ACTUATOR_FAULT | 动作阶段故障，错误码 2004 |

occurrence 表示每个任务中第几次匹配该类检查/阶段时触发，计数在每个任务开始时重置。
例如 DOWNLOCK_TIMEOUT occurrence=2 表示第二次下位锁确认失败；预动作确认也包含在内。
故障配置不会因任务结束自动清除。测试完务必配置：

```json
{"fault": "NONE", "occurrence": 1}
```

执行失败后就绪状态被清除，应使用新 taskId 重新初始化，再做正常任务测试。

## 9. 常见问题与返回码

| 现象 | 含义与处理 |
| --- | --- |
| HTTP 400，code=1001 | 参数不合法；查看 details，检查字段、枚举和 JSON 类型 |
| HTTP 401，code=1002 | 鉴权失败，检查 x-api-key |
| HTTP 409，code=1003 | 任务号已使用；查询旧任务或给真正的新任务换编号 |
| HTTP 409，code=2001 | 正在执行其他任务；等待或请求取消，不会自动排队 |
| HTTP 409，code=2003 | 初始化未完成或就绪状态已清除；先成功初始化 |
| FAILED，errorCode="2002" | 模拟安全联锁触发，检查模拟故障配置 |
| FAILED，errorCode="2004" | 模拟动作、目标确认或执行器失败；重启中断也使用该码 |
| HTTP 404，code=1001，task not found | 查询的任务不存在或连接了不同数据库/服务；这是当前实现的补充约定 |
| 查询 status=SUCCEEDED，但 exhausted 大于 0 | 模拟任务完成，但部分回调发送失败，不能据此判定 ELN 已收到结果 |
| docs 能打开但一直空白 | 页面脚本来自外部 CDN，浏览器可能无法加载；先检查 /health 或 /openapi.json |
| 浏览器无法连接 | 检查程序是否启动、地址/端口及访问路径；不是 taskType 参数错误 |
| 启动提示 address already in use | 端口已有进程使用，确认是否就是已运行服务，不要盲目停止其他进程 |

注意：不要沿用早期讨论中的 2005/9000 扩展码说明；本手册以上表所列当前代码为准。
业务码 code 是数字；回调 errorCode 是字符串或 null。
同一个 progress 的阶段回调不是错误。UTC 时间的 +00:00 也是合法带时区时间。

参数约束：

- taskId 为 1 至 128 个英文字母、数字、点、下划线或连字符，不使用中文、空格或斜杠。
- count 为 1 至 2147483647 的 JSON 整数，不能填字符串 "3"、小数或 true。
- 枚举大小写必须一致，不接受 V1.0 的 PEDAL_ROTATE、MLG_WOW、direction、wowState、action。
- 起落架 count、preAction、cycleMode 全部必填；其他额外字段会被拒绝。
- callbackUrl 为 HTTP/HTTPS 地址，不能含用户名、密码或 URL fragment。
- 初始化只使用初始化接口，不把 ROBOT_INITIALIZE 提交到控制任务接口。

## 10. 回调失败与重启

每条回调首次发送失败后，最多再试三次，默认间隔约为 1、2、4 秒，单次请求超时默认 3 秒。
次数耗尽后记录保留，不会自动重新执行任务。pending=0 仍需结合 exhausted 判断是否全部送达。
某条回调耗尽重试后，后续回调继续发送，因此 ELN 可能收到最终结果但缺少中间事件。

机器人任务数据库默认位于 `communication/eln_http_demo/data/interface-v1.5.sqlite3`。
模拟 ELN 默认数据库为工作目录下的 `data/eln.sqlite3`，可由 ELN_DATABASE 环境变量覆盖。
不要通过删除数据库解决任务号重复；这样会丢失记录和去重依据。
服务重启不会重放任务；未完成任务会记为失败，保留已保存的完成次数，并要求重新初始化。
仅使用一个服务进程和一个 Uvicorn worker 管理同一机器人任务库。

## 11. 接入真实 ELN 时

用真实 ELN 的回调 URL 替换本手册 localhost 示例；ELN 直接调用机器人接口，无须经过 docs。
停止使用本地模拟接收端并不改变机器人接口路径；但已经接收的任务保留原 callbackUrl，不会自动改地址。
机器人 X-API-Key 控制入站鉴权；当前出站回调仅设置 Content-Type，若真实 ELN 要求回调鉴权需补充支持。
只通过真实 ELN 下发并收到其回调确认，才算真实系统通信验收。当前所有机械与信号仍为模拟。

## 12. 一次人工调试的完成检查

- [ ] 健康检查显示 V1.5、mock、simulation=true。
- [ ] 初始化查询为 SUCCEEDED、progress=1。
- [ ] NWS 三组任务为 SUCCEEDED、progress=3。
- [ ] WOW 的 AIR 和 GROUND 分别完成、progress=1。
- [ ] 起落架完整循环、最终仅收起和预动作模式计数符合预期。
- [ ] 模拟 ELN 的 events 接口查到了对应任务最终回调。
- [ ] 回调投递 pending 最终归零，正常案例 exhausted=0。
- [ ] 长任务取消得到最终 CANCELLED，未完成的操作没有计数。
- [ ] 若测试过故障，恢复 NONE，并重新初始化成功。

本手册只说明操作方法，不表示上述检查已在当前运行实例上自动执行。
