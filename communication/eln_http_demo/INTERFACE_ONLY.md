# ELN V1.5 纯接口联调

本入口只完成 HTTP 请求、响应、模拟任务进度和回调，不连接机械臂、灵巧手、ROS2 或 DDS。
固定使用 mock 执行器，即使环境变量 ROBOT_BACKEND=ros2 也不会启用硬件后端。
此处的 SUCCEEDED 只代表接口模拟流程完成。
当前主要入口遵循 V1.5；原 robot_gateway.app 入口及 ROS2 演示保持 V1.0 兼容，不用于新版联调。

## 开机自启（机器人部署）

仓库提供 `deploy/eln_interface.service`。它只启动本页所述的纯接口模拟服务，
不会启动 ROS2、DDS、机械臂或灵巧手控制。安装并立即启动：

```bash
cd /home/unitree/rh56dfx/workspace
sudo install -m 0644 deploy/eln_interface.service /etc/systemd/system/eln_interface.service
sudo systemctl daemon-reload
sudo systemctl enable --now eln_interface.service
```

服务固定监听 `0.0.0.0:8090`，因此所有本机网卡的可达地址都可用于 ELN 请求或
`/docs` 页面。进程异常或正常退出后 systemd 都会自动拉起；人工执行
`systemctl stop` 时不会被自动拉起。检查状态和日志：

```bash
systemctl status eln_interface.service --no-pager
journalctl -u eln_interface.service -n 100 --no-pager
curl --noproxy '*' -fsS http://127.0.0.1:8090/health
```

当前真实机器人部署的 `execution/action_runtime_v3/config/interface_relay.json`
设置 `require_client_api_key=false`，ELN 访问 8090 无需携带 `X-API-Key`。
转发服务使用 `config/api.key` 认证本机内部 API；内部认证及执行前置条件检查仍然保留。
此配置修改后仅需重启 `eln_interface.service`。设为 `true` 可恢复客户端密钥校验。
下述 `ROBOT_API_KEY` 配置仅适用于纯模拟入口，不控制真实转发入口。

如需启用模拟入口的入站鉴权，在机器人上创建仅管理员可读的
`/etc/default/rh56dfx-eln-interface`（不要把密钥写入仓库）：

```bash
ROBOT_API_KEY=replace-with-the-real-key
```

修改后执行 `sudo systemctl restart eln_interface.service`。ELN 和 `/docs` 发出的业务请求
需要携带 `X-API-Key`；`GET /health` 仍可用于存活检查。

## 手动启动（仅调试）

使用在项目内重建的 `.venv`，在两个终端中分别执行。以下均在此目录运行：

```bash
cd /home/unitree/rh56dfx/workspace/communication/eln_http_demo
```

从工作区根目录按 [Python 环境恢复步骤](../../docs/python_environment.md)创建 `.venv` 并安装依赖。
启动模拟前确认 `../../execution/action_runtime_v3/config/interface_relay.json`
未启用；存在该文件时，入口会切换为真实转发。

终端一：启动模拟 ELN 回调接收端（真实 ELN 尚未部署时使用）。

```bash
.venv/bin/python -m uvicorn \
  pc_a_simulator.callback_server:app --host 127.0.0.1 --port 8091
```

终端二：启动机器人纯接口服务。

```bash
.venv/bin/python -m deployment.interface_service \
  --host 127.0.0.1 --port 8090
```

已启用 systemd 服务时不要再执行这条命令，否则会因 8090 端口已占用而退出。

文档：http://127.0.0.1:8090/docs 。健康状态：GET /health，应返回 backend=mock、simulation=true。
protocolVersion 应为 1.5；业务路径保持 /api/v1。
默认监听 0.0.0.0:8090，对外提供接口；仅本机调试时可使用 --host 127.0.0.1。
可通过 ROBOT_API_KEY 环境变量启用鉴权，请求头使用 X-API-Key。
这是 communication/eln_http_demo 的接口入口，鉴权与早期 scripts/eln_service.py 的 Bearer 方式不同，勿混用。

## 完整调用顺序

先 POST /api/v1/robot/initialize，请求体为：

```json
{
  "taskId": "INIT-001",
  "callbackUrl": "http://127.0.0.1:8091/eln/api/v1/robot-callbacks/task-status"
}
```

taskId、callbackUrl 均必填，不再自动生成任务号或使用服务默认回调地址。
返回 {"code":0,"message":"accepted","taskId":"INIT-001","status":"ACCEPTED"}。
POST /api/v1/tasks/INIT-001/query，等待 status=SUCCEEDED 后提交业务任务。

```json
{
  "taskId": "NWS-001",
  "taskType": "NWS_CYCLE",
  "callbackUrl": "http://127.0.0.1:8091/eln/api/v1/robot-callbacks/task-status",
  "parameters": {"count": 3}
}
```

将上面的 JSON POST 到 /api/v1/tasks，即刻返回 ACCEPTED，后台每 0.5 秒模拟一个阶段。
NWS_CYCLE 每组包含左旋、右旋两个阶段，整组完成才计数；请求不再携带 direction。
期间主动 POST callbackUrl，依次推送 ACCEPTED、RUNNING(progress=0)、
RUNNING(progress=1/2/3)、SUCCEEDED(progress=3)。--step-seconds 可调整模拟耗时。
每次回调均包含 taskId、taskType、status、progress、message、timestamp、errorCode。
ELN 应返回 HTTP 2xx 和 {"code":0}；本实现失败时最多重试三次，重试不重新执行任务。
重试次数属于实现策略，V1.5 未规定上限。去重键为 taskId + status + progress + timestamp。

NLG_WOW_OPERATION 参数为 {"wowSignal":"AIR"} 或 {"wowSignal":"GROUND"}，
模拟目标状态达成后 progress=1。初始化完成时也为 1。

GEAR_OPERATION 参数示例：

```json
{"count":5,"preAction":"EXTEND_DOWNLOCK","cycleMode":"FINAL_RETRACT_ONLY"}
```

三个字段均必填。preAction 为 NONE 或 EXTEND_DOWNLOCK，后者先模拟放下并确认下位锁，不计数。
cycleMode 为 FULL_CYCLE 时，每次模拟收起确认上位锁、放下确认下位锁后计数。
FINAL_RETRACT_ONLY 前 count-1 次为完整循环，最后一次仅收起确认上位锁，然后停止。
count=1 时最后仅收起模式正式计数只包含收起；预动作仍按 preAction 执行。
每次新任务需要唯一 taskId，count 必须为正整数；旧任务类型和旧参数在新版入口被拒绝。

POST /api/v1/tasks/NWS-001/query 可查询任务和回调投递情况，无须请求体。
POST /api/v1/tasks/NWS-001/cancel，请求体为 {"reason":"test cancel"}，
模拟等待可被取消中断，未完成的当前阶段或循环不计数，并推送 CANCELLED。
已完整完成最后一次操作则返回 SUCCEEDED。
可将 count 设置为 20，留出足够时间测试取消。

检查模拟 ELN 实际收到的回调：

```bash
curl --noproxy '*' -X POST http://127.0.0.1:8091/eln/api/v1/tasks/NWS-001/events
```

任务和回调使用 SQLite 持久化，服务重启后不会重放动作；必须重新初始化。
新建任务号后再重试，不能反复使用 INIT-001/NWS-001。
默认新版任务库为 data/interface-v1.5.sqlite3，不自动复用旧版任务库。
显式指定包含不兼容旧请求的数据库时启动会报错，旧记录保留，不自动删除。
模拟 ELN 使用 events_v15 表，启动时复制旧 events 回调记录，旧表也保留。
回调地址替换成真实 ELN 的 URL 后，调用流程保持不变。

## 错误码补充约定

1001 参数错误、1002 未授权、1003 重复任务、2001 忙、2002 安全条件不满足、
2003 初始化未完成、2004 执行器故障。纯模拟不读取实际安全联锁。
V1.5 未定义任务不存在和通用内部错误，本实现暂保留扩展码：
2005 任务不存在（HTTP 404），9000 内部错误或重启中断；需与 ELN 方确认。
这两个码不属于原协议规定；不会把任务不存在误报为执行机构故障。

## 验证

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest tests -q
```

test_interface_http.py 会启动两个临时 HTTP 服务，实际发送四类任务，
验证受理响应、查询结果以及 ELN 收到的完整回调，结束时自动关闭服务。
关闭 pytest 插件自动加载是为了避免本机 ROS2 测试插件干扰纯 HTTP 测试。
