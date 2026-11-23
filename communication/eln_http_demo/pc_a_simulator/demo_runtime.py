"""Own two loopback HTTP servers for the desktop simulator."""
import socket
import threading
import time
from pathlib import Path
from uuid import uuid4

import httpx
import uvicorn

from robot_gateway.app import create_app
from robot_gateway.config import Settings
from robot_gateway.models import TERMINAL
from .callback_server import create_app as create_callback_app


class DemoRuntime:
    mode_label = "本机模拟 · 未连接真实机器人"

    def __init__(self, directory=None, step_seconds=0.5):
        self.directory = Path(directory) if directory else (
            Path(__file__).resolve().parents[1] / "data" / "desktop_demo" / uuid4().hex[:12])
        self.step_seconds = step_seconds
        self.servers = []
        self.client = httpx.Client(timeout=5, trust_env=False)

    def _serve(self, app, preferred_port):
        sock = socket.socket()
        try:
            try:
                sock.bind(("127.0.0.1", preferred_port))
            except OSError:
                sock.bind(("127.0.0.1", 0))
            sock.listen(128)
            port = sock.getsockname()[1]
            server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
            thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
            self.servers.append((server, thread, sock))
            thread.start()
            deadline = time.monotonic() + 15
            while not server.started:
                if not thread.is_alive() or time.monotonic() > deadline:
                    raise RuntimeError("模拟服务启动失败，请查看启动窗口中的错误。")
                time.sleep(0.05)
            return f"http://127.0.0.1:{port}"
        except Exception:
            sock.close()
            raise

    def start(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        try:
            self.receiver_url = self._serve(create_callback_app(str(self.directory / "eln.db")), 18001)
            self.callback_url = self.receiver_url + "/eln/api/v1/robot-callbacks/task-status"
            settings = Settings(database=str(self.directory / "gateway.db"), backend="mock",
                                callback_url=self.callback_url, mock_step_seconds=self.step_seconds)
            self.gateway_url = self._serve(create_app(settings), 18000)
        except Exception:
            self.close()
            raise

    def post(self, url, body=None):
        response = self.client.post(url, json=body)
        data = response.json()
        if response.is_error or data.get("code") != 0:
            raise RuntimeError(f"{data.get('code', response.status_code)}: {data.get('message', '请求失败')}")
        return data

    def health(self):
        response = self.client.get(self.gateway_url + "/health")
        response.raise_for_status()
        return response.json()

    def submit(self, kind, parameters):
        task_id = "GUI-" + uuid4().hex[:12]
        if kind == "ROBOT_INITIALIZE":
            self.post(self.gateway_url + "/api/v1/robot/initialize",
                      dict(taskId=task_id, callbackUrl=self.callback_url))
        else:
            self.post(self.gateway_url + "/api/v1/tasks", dict(taskId=task_id, taskType=kind,
                      parameters=parameters, callbackUrl=self.callback_url))
        return task_id

    def configure_simulation(self, fault="NONE", occurrence=1):
        return self.post(self.gateway_url + "/api/v1/simulation/config", dict(fault=fault, occurrence=occurrence))

    def cancel(self, task_id):
        return self.post(f"{self.gateway_url}/api/v1/tasks/{task_id}/cancel", {"reason": "desktop demo cancelled"})

    def query(self, task_id):
        return self.post(f"{self.gateway_url}/api/v1/tasks/{task_id}/query")

    def events(self, task_id):
        return self.post(f"{self.receiver_url}/eln/api/v1/tasks/{task_id}/events")["events"]

    def run_task(self, kind, parameters, stop, notify):
        task_id = self.submit(kind, parameters)
        total = parameters.get("count", 1)
        notify("active", (task_id, kind, total))
        seen = set()
        cancel_sent = False
        while True:
            if stop.is_set() and not cancel_sent:
                self.cancel(task_id)
                cancel_sent = True
            state = self.query(task_id)
            notify("state", state)
            for event in self.events(task_id):
                key = (event["status"], event["progress"], event["timestamp"])
                if key not in seen:
                    seen.add(key)
                    notify("event", event)
            if state["status"] in TERMINAL and state["callbackDelivery"]["pending"] == 0:
                if state["callbackDelivery"]["exhausted"]:
                    raise RuntimeError("任务已结束，但部分 ELN 回调未送达。")
                return state
            time.sleep(0.1)

    def close(self):
        # Stop the gateway before its callback receiver so final events can still arrive.
        for server, thread, sock in reversed(self.servers):
            server.should_exit = True
            thread.join(timeout=15)
            sock.close()
        self.servers.clear()
        self.client.close()
