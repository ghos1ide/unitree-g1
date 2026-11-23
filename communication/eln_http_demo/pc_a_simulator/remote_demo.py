"""Desktop runtime connected to the robot, with callbacks over an SSH tunnel."""
import json
import subprocess
import time
from pathlib import Path

from .callback_server import create_app as create_callback_app
from .demo_runtime import DemoRuntime


class RemoteDemoRuntime(DemoRuntime):
    mode_label = "机器人 HTTP 通信 · 模拟动作（不会运动）"

    def __init__(self, callback_port=18081):
        super().__init__()
        self.callback_port = callback_port
        self.tunnel = None
        self.tunnel_log = None
        self.ssh = r"C:\Windows\System32\OpenSSH\ssh.exe"

    def start(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        try:
            path = Path(__file__).resolve().parents[1] / "data" / "robot_connection.json"
            config = json.loads(path.read_text(encoding="utf-8"))
            self.gateway_url = config["gateway_url"]
            self.api_key = config["api_key"]
            health = self.health()
            if health.get("protocolVersion") != "1.5":
                raise RuntimeError("机器人仍运行旧协议，请先更新 V1.5 网关和 ROS2 接口。")
            if health["backend"] != "mock" and not health.get("simulation", False):
                raise RuntimeError("目标服务不是模拟模式，已停止连接。")
            if health["backend"] == "ros2":
                self.mode_label = "机器人 HTTP + ROS2 Action · 假动作（不会运动）"
            self.receiver_url = self._serve(create_callback_app(str(self.directory / "eln.db")), 18001)
            local_port = self.receiver_url.rsplit(":", 1)[1]
            self.tunnel_log = (self.directory / "ssh.log").open("w", encoding="utf-8")
            self.tunnel = subprocess.Popen([self.ssh, "-N", "-T", "-o", "BatchMode=yes",
                "-o", "ConnectTimeout=10", "-o", "ExitOnForwardFailure=yes",
                "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=2",
                "-R", f"127.0.0.1:{self.callback_port}:127.0.0.1:{local_port}", config["ssh_target"]],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=self.tunnel_log,
                creationflags=subprocess.CREATE_NO_WINDOW)
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if self.tunnel.poll() is not None:
                    raise RuntimeError("SSH 回调通道建立失败。请关闭其他机器人模拟窗口，确认 SSH 可连接后重试。")
                check = subprocess.run([self.ssh, "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
                    config["ssh_target"], f"curl --max-time 2 -fsS http://127.0.0.1:{self.callback_port}/openapi.json"],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10,
                    creationflags=subprocess.CREATE_NO_WINDOW)
                if check.returncode == 0 and self.tunnel.poll() is None:
                    break
                time.sleep(0.3)
            else:
                raise RuntimeError("机器人到电脑的回调通道验证超时。")
            self.callback_url = f"http://127.0.0.1:{self.callback_port}/eln/api/v1/robot-callbacks/task-status"
        except Exception:
            self.close()
            raise

    def post(self, url, body=None):
        headers = {"X-API-Key": self.api_key} if url.startswith(self.gateway_url + "/") else {}
        response = self.client.post(url, json=body, headers=headers)
        data = response.json()
        if response.is_error or data.get("code") != 0:
            raise RuntimeError(f"{data.get('code', response.status_code)}: {data.get('message', '请求失败')}")
        return data

    def close(self):
        if self.tunnel is not None:
            self.tunnel.terminate()
            self.tunnel.wait(timeout=10)
            self.tunnel = None
        if self.tunnel_log is not None:
            self.tunnel_log.close()
            self.tunnel_log = None
        super().close()
