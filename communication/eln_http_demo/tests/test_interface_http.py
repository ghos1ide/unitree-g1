from contextlib import contextmanager
import socket
import threading
import time

import httpx
import uvicorn

from deployment.interface_service import create_interface_app
from pc_a_simulator.callback_server import create_app as create_callback_app
from robot_gateway.config import Settings


@contextmanager
def serve(app):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started:
            assert thread.is_alive() and time.monotonic() < deadline, "Server did not start"
            time.sleep(0.01)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()
        assert not thread.is_alive(), "Server did not stop"


def test_real_http_tasks_and_callbacks_without_hardware(tmp_path, monkeypatch):
    monkeypatch.setenv("ROBOT_BACKEND", "ros2")
    settings = Settings.from_env()
    settings.database = str(tmp_path / "robot.sqlite3")
    settings.mock_step_seconds = 0.01
    settings.api_key = "integration-key"
    with serve(create_callback_app(str(tmp_path / "eln.sqlite3"))) as eln:
        callback = eln + "/eln/api/v1/robot-callbacks/task-status"
        settings.callback_url = callback
        with serve(create_interface_app(settings)) as robot, httpx.Client(trust_env=False, timeout=3) as client:
            health = client.get(robot + "/health").json()
            assert health["backend"] == "mock" and health["simulation"] is True
            assert client.post(robot + "/api/v1/robot/initialize", json={}).status_code == 401
            client.headers["X-API-Key"] = "integration-key"
            cases = [
                ("init", "ROBOT_INITIALIZE", {}, 1),
                ("nws", "NWS_CYCLE", {"count": 3}, 3),
                ("wow", "NLG_WOW_OPERATION", {"wowSignal": "AIR"}, 1),
                ("gear", "GEAR_OPERATION", {"count": 2, "preAction": "EXTEND_DOWNLOCK", "cycleMode": "FINAL_RETRACT_ONLY"}, 2),
            ]
            for task_id, kind, params, count in cases:
                if task_id == "init":
                    response = client.post(robot + "/api/v1/robot/initialize", json={"taskId": task_id, "callbackUrl": callback})
                else:
                    response = client.post(robot + "/api/v1/tasks", json=dict(
                        taskId=task_id, taskType=kind, callbackUrl=callback, parameters=params))
                assert response.status_code == 200
                assert response.json() == dict(code=0, message="accepted", taskId=task_id, status="ACCEPTED")
                deadline = time.monotonic() + 5
                while True:
                    events = client.post(eln + f"/eln/api/v1/tasks/{task_id}/events").json()["events"]
                    if events and events[-1]["status"] == "SUCCEEDED":
                        break
                    assert time.monotonic() < deadline, "Final callback not received"
                    time.sleep(0.02)
                assert events[0]["status"] == "ACCEPTED"
                assert events[1]["status"] == "RUNNING" and events[1]["progress"] == 0
                assert events[-1]["progress"] == count
                assert all(e["taskId"] == task_id and e["taskType"] == kind and e["errorCode"] is None for e in events)
                progress = [e["progress"] for e in events if e["status"] == "RUNNING"]
                assert progress == sorted(progress)
                assert list(dict.fromkeys(progress)) == list(range(count + 1))
                state = client.post(robot + f"/api/v1/tasks/{task_id}/query").json()
                assert state["status"] == "SUCCEEDED" and state["progress"] == count
            duplicate = client.post(robot + "/api/v1/robot/initialize", json={"taskId": "init", "callbackUrl": callback})
            assert duplicate.json()["code"] == 1003
