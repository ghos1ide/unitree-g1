# Unitree G1 · Inspire RH56DFX 工作区

从 `/home/unitree/unitree-migration/rh56dfx/workspace` 恢复手控、网页、RGB-D 识别、ELN V1.5 和动作执行源码。手册、视觉权重、当前选定动作包及配套的起落架和 D 环协议已迁入。**编译和离线测试不代表实机就绪**；未安装、启用或启动机器人服务，旧目录保持原样。

## 本机构建与验证

原生构建使用本机现有的 Unitree SDK2 和 yaml-cpp，无需替换系统 SDK 或 DDS 配置。新建构建目录时可显式启用已恢复的组件；这些命令不访问设备。

```bash
cd /home/unitree/rh56dfx/workspace
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release \
  -DRH56_BUILD_HAND=ON -DRH56_BUILD_HAND_WEB=ON -DRH56_BUILD_VISION=ON
cmake --build build -j2
(cd build && ctest --output-on-failure)
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s recognize/pedal_center_package_v1/tests
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover \
  -s execution/action_runtime_v3/deployment/rail_jetson -p test_gear_progress.py
./scripts/capture_head_camera.sh --help
./scripts/record_right_wrist_camera.sh --help
```

实际头部抓拍需要可用的 `videohub` 和正确的 DDS 网卡。右腕录像需要 OpenCV、GStreamer 和对应的 V4L2 设备；旧序列号保留在脚本默认值中，换设备时应使用 `--device` 指定目标的稳定路径。两者都没有在本次迁移中访问相机。

## ELN 接口

V1.5 网关、模拟客户端、监控、测试和 `robot_interfaces` 源码位于 `communication/eln_http_demo/`。真实执行端位于 `execution/action_runtime_v3/`。旧转发配置及 systemd drop-in 只保留在 [`deploy/reference/`](deploy/reference/README.md)，没有激活对实机执行端的转发。V1.5 离线联调见[接口说明](communication/eln_http_demo/INTERFACE_ONLY.md)。

`scripts/eln_service.py` 是独立的早期 V1 仿真，不替代 V1.5。两者可共用 `communication/eln_http_demo/.venv`。CMake 优先使用这个解释器，也可通过 `-DELN_PYTHON_EXECUTABLE=/path/to/python` 指定。详见 [ELN V1 接口文档](docs/eln_service_api.md)。

## 环境恢复

`migration-dependencies` 清单已通过 SHA-256 校验。ELN 的隔离 Python 3.8 环境及 pip 已建好；项目依赖仍待安装。补齐 ARM64/Python 3.8 wheel 后，在项目根目录运行 `bash scripts/setup_eln_env.sh --wheelhouse /path/to/eln-wheelhouse`。视觉使用独立的 `.envs/unitree`；所需 Conda 包、Jetson 专用 wheel 及核验方法见 [Python 环境恢复步骤](docs/python_environment.md)。不需要替换系统 Python、CUDA 或 ROS。

## 剩余缺件

上次清单中的核心源码、网页、模型和 ELN drop-in 已补齐。在本机部署或做相应测试还需要以下内容：

| 类别 | 仍缺或需要重新配置 |
| --- | --- |
| ELN Python 环境 | 工作区内的 `.venv` 已准备好，仍缺清单固定的 FastAPI、Pydantic 2、Uvicorn、HTTPX 等应用安装包。本次会话无法解析软件源域名，需要离线 wheel 仓库或能访问软件源的环境。 |
| RGB-D 推理环境 | 缺 PyTorch、Ultralytics、`pyrealsense2`。系统 OpenCV/NumPy 能跑模拟测试，但低于 `recognize/pedal_center_package_v1/requirements.txt` 的推理依赖要求。实际推理需准备兼容的 ARM64 环境，并用 `vision_service --python` 指向它。 |
| 设备和外参 | 本次环境未见 `/dev/serial/by-id/` 和 `/dev/v4l/by-id/`。待核对左右手串口、USB 相机序列号、权限、DDS 接口和占用。未见 `config/head_camera_to_base.json`；需要输出机器人目标坐标时，须在目标机重新标定或验证外参。 |
| ELN 生产配置 | `execution/action_runtime_v3/config/{api.key,native.token}` 是旧凭据，未复制；在目标机另行配置。`config/interface_relay.json` 暂不激活，参考副本为 `deploy/reference/action_runtime_v3-interface_relay.json`，部署时另审鉴权和转发设置。 |
| 旧回归样本 | 补传包中未找到早期文档引用的 `execution/action_runtime_v3/data/rail_deployment/20260921_offline_v1/`（旧 `bundle.json` 及两个对照报告）。不影响当前 `config/selected_bundle.json`，仅在复现该次离线回归时需要。 |

旧机标定保留在 `config/head_camera_intrinsics.json`，五个注册姿态和抓握参数在 `config/source_robot/`，没有写入运行时 `data/`。启用实机前须复核串口左右手映射、设备权限与占用、相机标定和 DDS 网卡，再决定哪些旧参数适用。

旧 PID、SQLite/WAL、日志、录像、虚拟环境、旧构建产物及非当前的候选轨迹和传输归档未迁入；它们不是当前动作包的运行依赖。生产任务数据库如需迁移，应从旧机取得一致性备份。补传源的历史清单与当前文件并非同一版本：`action_runtime_v3/runtime_manifest.json` 的 13 项及 `action_runtime_v3/native_ws/source_hashes.json` 的网关源码在源目录中已与旧哈希不同，保留补传原件和旧清单，不重写哈希。

可参阅[旧工作区说明](../../unitree-migration/rh56dfx/workspace/README.md)及[旧机依赖清单](../../unitree-migration/migration-dependencies/README.md)。补传后的 `migration-config` 通过了 SHA-256 校验；它仍是参考，未运行其中的系统安装步骤。
