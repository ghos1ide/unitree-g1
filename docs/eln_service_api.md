# ELN V1 通信服务

依据附件《人形机器人 ELN HTTP API 协议 V1.0》实现任务接收、状态回调、查询和取消。
当前版本完成通信与模拟执行，**尚未接入真实机器人业务动作，不能用于真实作业验收**。
HTTP 层使用 FastAPI + Uvicorn，任务存储与回调使用 Python 标准库。
迁移后的工作区使用 `communication/eln_http_demo/.venv`，也可使用已准备好的 Python 环境。
`requirements-eln.txt` 固定了支持 Python 3.8 的框架依赖。

## 启动与联调

在项目根目录按[依赖清单](python_environment.md)准备环境：

```bash
bash scripts/setup_eln_env.sh --wheelhouse /path/to/eln-wheelhouse
```

启动仿真时将回调地址替换为测试接收器的地址。使用独立的仿真数据库：

```bash
communication/eln_http_demo/.venv/bin/python scripts/eln_service.py --simulate \
  --database data/eln/simulation.sqlite3 \
  --callback-url http://127.0.0.1:8091/eln/api/v1/robot-callbacks/task-status
```

默认监听 `127.0.0.1:8090`。`--simulate` 不连接硬件，动作及初始化成功仅表示模拟成功。
交互式接口文档为 `http://127.0.0.1:8090/docs`，OpenAPI JSON 为 `/openapi.json`。
文档可公开读取；启用令牌时，在文档的 Authorize 中填写令牌后才能调用业务接口。
不传 `--simulate` 时执行器禁用，任务返回 2002，不会产生运动。
`--simulation-delay` 指定模拟单次动作耗时，默认 0.2 秒。

局域网联调使用 `--bind 0.0.0.0`，必须设置环境变量 `ELN_API_TOKEN`；
ELN 请求携带 `Authorization: Bearer <token>`。令牌不要写入版本库。
HTTPS 由部署侧反向代理终止，本服务本身提供 HTTP；不应直接暴露公网。
回调 HTTPS 使用系统证书校验，不跟随重定向。
请求中的 callbackUrl 必须与 `--callback-url` 或可重复指定的
`--allow-callback-url` 完全匹配，防止任意地址回调。

初始化示例：

```bash
curl -sS http://127.0.0.1:8090/api/v1/robot/initialize \
  -H 'Content-Type: application/json' -d '{"taskId":"INIT-001"}'
curl -sS http://127.0.0.1:8090/api/v1/tasks/INIT-001/query \
  -H 'Content-Type: application/json' -d '{}'
```

必须等初始化 SUCCEEDED 后再提交控制任务：

```bash
curl -sS http://127.0.0.1:8090/api/v1/tasks \
  -H 'Content-Type: application/json' \
  -d '{"taskId":"PEDAL-001","taskType":"PEDAL_ROTATE","parameters":{"direction":"LEFT","count":20}}'
curl -sS http://127.0.0.1:8090/api/v1/tasks/PEDAL-001/query \
  -H 'Content-Type: application/json' -d '{}'
curl -sS http://127.0.0.1:8090/api/v1/tasks/PEDAL-001/cancel \
  -H 'Content-Type: application/json' -d '{"reason":"ELN operator cancelled"}'
```

## 行为约定

| 接口 | 说明 |
| --- | --- |
| POST /api/v1/robot/initialize | 接收初始化任务，支持 taskId、callbackUrl |
| POST /api/v1/tasks | taskId、taskType、parameters；callbackUrl 可使用服务默认值 |
| POST /api/v1/tasks/{taskId}/query | 请求体 `{}`，返回 code 和完整状态字段 |
| POST /api/v1/tasks/{taskId}/cancel | 可选 reason；返回当前状态，受控停止完成后才回调 CANCELLED |

任务参数：PEDAL_ROTATE 为 direction=LEFT/RIGHT、整数 count=1..10000；
MLG_WOW 为 wowState=ON/OFF；GEAR_OPERATION 为 action=RETRACT/EXTEND；
ROBOT_INITIALIZE 为空对象。taskId 限制为 1..128 个英文字母、数字、点、下划线或连字符。

同一时刻只执行一个任务，忙时返回 2001，不排队。重复 taskId 返回 1003；
状态保存在 `data/eln/tasks.sqlite3`，可通过 `--database` 调整位置。
数据库必须保留，删除数据库会丢失任务去重记录。同目录 `.lock` 文件的进程锁保证一个数据库
仅由一个服务进程使用，重复启动会报错；模拟数据库不能用于真实执行。

状态顺序为 ACCEPTED → RUNNING → SUCCEEDED/FAILED/CANCELLED。
progress 是完成次数，非百分比；失败和取消保留实际已完成次数。
最终状态不再改变。取消与最后一次动作完成同时发生时，已全部完成的任务记为 SUCCEEDED。
错误回复包含数值 code 与 message；失败回调 errorCode 使用对应业务错误码的字符串。
成功 HTTP 状态为 200，未授权为 401，业务参数/执行前置条件错误为 400，
未知路径为 404，方法不允许为 405，未知内部错误为 500。
FastAPI 字段校验错误统一转换为 code=1001，而非默认的 422 响应。

每次状态变化与回调事件在同一个数据库事务中保存。回调成功要求 HTTP 2xx 且 JSON code=0。
首次失败后最多重试三次，等待间隔为 1、2、4 秒，单次网络操作超时 3 秒。
同一任务按事件顺序发送；重试使用原始 payload（含原始 timestamp）。
某事件重试耗尽后保留在数据库并记录错误，后续事件继续发送；当前版本没有自动重放耗尽事件的接口。
ELN 应按 taskId + status + timestamp 去重，否则多次 RUNNING 进度会被错误合并。
回调中断不触发动作重放，ELN 可主动查询最终状态。

服务重启时不会重放动作，未终结任务记为 FAILED/9000，保留已记录进度；
动作实际结果需要人工核查。每次启动都必须重新初始化。
执行失败也会清除初始化就绪状态。SIGINT/SIGTERM 请求受控取消并等待执行线程结束；
未发送回调保存在数据库，下次启动继续尝试，已耗尽事件除外。

## 协议歧义与暂定解释

1. 初始化示例由 ELN 提供 taskId，正文又称机器人生成：优先保留请求 taskId，缺省才生成 INIT-UUID。
2. 初始化示例没有 callbackUrl：使用服务默认地址，也接受请求显式提供白名单内地址。
3. MLG_WOW 的 JSON 示例为空，但参数表要求 wowState：按参数表强制要求 ON/OFF。
4. 回调去重两处表述不同：采用 taskId + status + timestamp，以保留每次 RUNNING 进度。
5. 查询、取消响应和 HTTP 错误状态未定义：暂按上面的行为约定，需与 ELN 方确认。
6. 鉴权方式、任务号字符集、次数上限、单实例部署和 HTTPS 终止方式属于本实现补充约定。

## 真实动作接入边界

Service 接收 executor 对象。`check(task_type, parameters)` 在接收及每次动作前检查就绪和安全联锁，
失败抛出 ApiError(2002, ...)。`step(task_type, parameters, cancel_event)` 执行一次动作：
完整完成才返回 True；收到取消后完成受控停止才返回 False；异常抛出失败。
执行器必须在动作过程中检查安全条件和取消信号，并在任何异常返回前确保受控停止，
不能把“命令已发出”当成“动作已完成”。当前命令行仅提供禁用和模拟两个执行器。

真实执行器还需要：初始化自检及回零实现、脚蹬单次曲线、WOW/起落架动作程序、
限位/舱门/液压/WOW/人员区域信号、动作超时与受控停止确认方式。
涉及灵巧手的操作只能通过现有 C++ `rh56::HandClient` 使用 hand_service，
不能在此服务中直接打开串口，也不能另建手部协议或 DDS 控制通道。
未取得这些接口和实机验证结果前，不应启用真实动作。
HTTP 取消不能替代独立硬件急停或断电。

## 验证

```bash
communication/eln_http_demo/.venv/bin/python tests/eln_service_test.py
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j2
(cd build && ctest --output-on-failure)
```

Python 测试在本机临时端口运行模拟 HTTP 服务和回调接收端，测试数据库位于临时目录，
测试结束自动清理，不读取硬件、不执行真实动作。
CMake 默认优先使用 `communication/eln_http_demo/.venv`，也可通过 `-DELN_PYTHON_EXECUTABLE=/path/to/python`
指定环境；依赖缺失时会明确警告并跳过 ELN 测试，不影响原有 C++ 测试。
