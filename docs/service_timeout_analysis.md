# 灵巧手与视觉服务调用超时分析

分析日期：2026-09-11。基于当前源码、已安装 SDK、运行进程和本次启动日志。

## 结论

两个服务当前均在运行。本次直接访问视觉 worker，状态查询约 1 ms，单次识别约 130 ms，其中 `inference_ms` 为 116.64 ms，均成功。尚未获得同事的接口名、返回码、客户端配置和故障时间，也未从同事机器复现，因此不能把某一项直接认定为本次故障的唯一根因。

优先检查三个方向：客户端默认仅等待 1 秒；同步长请求导致后续请求排队；DDS 网卡和启动就绪状态。代码已确认前两类触发条件存在，运行日志已确认发生过第三类启动失败。

## 1. RPC 超时与服务耗时不匹配

- 已安装 `/usr/local/include/unitree/robot/client/client_base.hpp:14` 定义 `ROBOT_CLIENT_TIMEOUT = 1000000`，注释明确默认 1 秒。
- `src/rh56/hand_client.hpp:15` 和 `src/vision/vision_client.hpp:14` 的初始化均没有设置 `SetTimeout`。只调用 `Init()` 的集成方会继承 SDK 默认值。
- 手动作执行在 `src/hand_service.cpp:409` 同步等待各组关节启动延时，全部命令发送后才返回。`src/rh56/persistent_store.hpp:23` 允许最大延时 3000 ms。因此，一个合法的 3 秒延时动作就足以超过默认 1 秒 RPC 等待时间，另有串口和排队开销。
- `ExecutePose(..., timeout_ms=5000)` 中的参数不会设置 RPC 等待时间。`src/hand_service.cpp:427` 在最后一组命令发送后设置控制看门狗；客户端仍须另设 `SetTimeout`。普通位置命令的 `timeout_ms` 同样是控制权/指令保活时限，不是 RPC 超时。
- 视觉 `src/vision_service.cpp:55` 默认允许等待 worker 60000 ms；`vision_runtime.py:227` 仅等相机帧就允许 5000 ms，随后还有两阶段推理、后处理和可选文件保存。
- 模型在初始化时加载，但没有显式运行两阶段推理预热。`warmup_frames` 仅用于相机帧预热，不能保证第一次模型推理小于 1 秒。冷启动具体耗时本次未测。

文档其实已有设置示例：手动作 `docs/hand_service_api.md:465` 使用 `SetTimeout(8.0f)`，视觉 `docs/vision_service_api.md:129` 使用 `SetTimeout(60.0F)`。但这些约束只存在于示例中，封装本身没有落实。`src/hand_web_control.cpp:1127` 已设置 8 秒，所以若故障来自现有 Web 控制端，不能简单归因于默认 1 秒。

视觉示例的外层 60 秒与内部 socket 的 60 秒也没有预留返回余量。worker 真正达到超时上限时，RPC 客户端可能先超时，收不到服务生成的 worker 错误响应。内部 socket 超时还是单次收发等待限制，不能作为包括锁等待在内的整个调用的严格截止时间。

## 2. 同步处理与排队会放大偶发慢请求

视觉链路：DDS RPC -> C++ `Forward` -> `UnixWorkerClient::Call` -> Python worker -> 相机与模型。

- `src/vision_service.cpp:236` 在整个 socket 请求过程中持有同一个 mutex。`RecognizeOnce` 和 `GetStatus` 都调用这个对象，所以一次慢识别会阻塞状态查询。
- Python worker 实际使用 `ThreadingMixIn`（`vision_worker.py:122`），并非单线程服务器；但 C++ 外层锁已经把正常 RPC 请求串行化。Python 识别自身还通过 `vision_runtime.py:224` 的 `_capture_lock` 串行访问相机与模型。
- 两个服务均使用 SDK 普通请求队列，运行日志显示 `enable proirity queue:0`；SDK `server_stub.hpp` 定义普通队列及其处理线程。手动作回调直接同步执行延时动作，存在后续查询、控制和 Hold 请求被前面的长动作拖延的风险。本次未做 DDS 并发压测。
- 在串行路径中，后续请求的等待时间包含前面请求的累计耗时。客户端超时后重试会增加队列压力。
- 业务代码没有把客户端的 RPC 截止时间传入执行逻辑，也没有按 `request_id` 去重。已开始执行的动作或推理不会因为调用方停止等待而自动取消。手部动作不能在超时后未经状态确认就盲目重试。

因此，“识别时连状态查询也超时”“执行延时动作时其他接口偶发超时”与当前实现一致。单纯增加客户端等待时间只能缓解，不能解决队列积压。

## 3. 已观察到 DDS 启动时网卡未就绪

本次启动日志明确记录：

```text
hand_service[964]: eth0: does not match an available interface.
hand_service[964]: Failed to create domain explicitly.
hand_service.service: Main process exited, code=exited, status=1/FAILURE
hand_service.service: Scheduled restart job, restart counter is at 1.
```

随后服务重启成功。当前实际启动参数中，两个服务都为 `--network eth0`；当前 `eth0` 地址为 `192.168.123.164/24`，`wlan0` 为 `192.168.100.100/24`。

- `deploy/hand_service.service:3` 的 `After=network-online.target` 在这次启动中没有保证 `eth0` 已可供 DDS 使用。
- 两个 unit 都是 `Type=simple`。systemd 显示 active 不代表 DDS 已完成初始化、发现和业务就绪；视觉服务还要先初始化 Python worker、模型和相机。
- 若调用端在启动后立即调用，可能正好落在服务初始化或重启窗口。若同事经 Wi-Fi 接入，而服务绑定机器人有线网卡，则须核对所选客户端网卡、DDS domain、路由及发现报文可达性；不能凭进程存在断定网络可达。
- 当前源码两服务的 DDS domain 均为 0。远端客户端要保持一致；远端网卡名称应选它自身实际连通的网卡，不一定也叫 `eth0`。

日志包含 1970 与 2026 年的时间跳变，实际故障时间关联需谨慎。本次没有证据表明时间跳变直接引起 RPC 超时。

## 4. 手部串口超时与 RPC 超时应分开判断

- `src/rh56/transport.hpp:88`、`:97` 的寄存器读取等待上限为 50 ms，`:142` 的写确认等待为 100 ms。
- `src/rh56/hand_controller.hpp` 在串口访问期间持有控制器 mutex，后台刷新与 RPC 会竞争同一把锁；完整状态刷新包含多次串口读取。延迟和失败可扩大接口耗时，但本次日志未见对应串口故障，不能据此断言硬件通信异常。
- `src/rh56/transport.hpp:132` 的 `write`/`tcdrain` 位于读取截止时间之外，所以 50/100 ms 并非整个串口事务的严格上限。
- SDK `3104` 是 RPC 等待超时（`/usr/local/include/unitree/robot/internal/internal_error.hpp:14`）；手服务业务码 `200` 是串口通信失败类别（`src/rh56/hand_types.hpp:21`）。协议校验等读取失败也可能归入这个业务码，不能只看名称。
- 两个客户端 `CallAndDecode` 将传输错误和业务错误都作为整数返回。排查时必须同时记录返回码、业务 message、接口和实际耗时；传输失败时 reply 并未被成功解码，不能使用旧 reply 判断本次结果。

## 建议排查与修复顺序

1. 获取一次实际故障的接口名、完整返回码/message、耗时、调用频率、并发数、客户端 `SetTimeout`、网卡和 domain。约 1 秒稳定失败首先核对默认 RPC 超时；启动阶段所有接口失败首先核对 DDS 就绪与网络。
2. 集成端明确设置超时。手延时动作可先采用现有文档的 `8.0f` 秒；视觉应建立“内部整体截止时间小于外层 RPC 等待时间”的预算，并计入排队余量。使用浮点秒数形式，注意 SDK 还存在整数重载。
3. 将手动作执行改为有界任务调度和任务状态查询，快速回复受理结果；Hold/取消应具备不会被长动作阻塞的处理路径。视觉状态查询独立于识别锁，识别队列设置容量和忙碌响应，避免无限等待。
4. 加入请求过期检查和动作去重，防止已超时请求继续积压或被重复执行。推理阻塞还需要 worker 恢复策略；关闭 socket 本身不会取消模型执行。
5. 为启动流程增加指定网卡就绪检查与业务 readiness 探测；客户端在服务发现完成后调用。`TimeoutStartSec` 不等于业务 RPC 超时。
6. 增加按 `request_id` 记录的分阶段耗时：DDS 收到请求、队列等待、锁等待、串口/取帧、推理、保存、回复。现有 `inference_ms` 只覆盖推理和三维投影，不包含整个识别调用。

## 本次验证范围

- 读取当前服务状态、启动命令、网卡地址、本次启动日志、源码及 SDK 头文件。
- 通过已运行 worker 的 Unix socket 查询一次状态并执行一次 `save_artifacts=false` 的识别；识别成功，未保存图像文件。
- 该测量绕过 C++ DDS RPC 层，只说明当时 worker 正常，不能证明远端 DDS 链路、冷启动或并发情况下无问题。
- 未重启服务，未直接访问手部串口，未发送手部运动命令，未修改实现或部署配置，未编译或运行测试套件。本次仅新增分析报告。
