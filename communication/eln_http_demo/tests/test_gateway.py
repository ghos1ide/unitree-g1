import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient

from pc_a_simulator.callback_server import create_app as callback_app
from robot_gateway.adapters import LocalExecutor, MockControlAdapter
from robot_gateway.app import create_app
from robot_gateway.config import Settings
from robot_gateway.models import TaskRequest
from robot_gateway.store import Store

CALLBACK = "http://eln.test/eln/api/v1/robot-callbacks/task-status"


def payload(task_id, task_type="ROBOT_INITIALIZE", parameters=None):
    return dict(taskId=task_id, taskType=task_type, parameters=parameters or {}, callbackUrl=CALLBACK)


def wait_for(client, task_id, terminal=True, delivered=False):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = client.post(f"/api/v1/tasks/{task_id}/query").json()
        done = result["status"] in {"SUCCEEDED", "FAILED", "CANCELLED"}
        if (done or not terminal) and (not delivered or result["callbackDelivery"]["pending"] == 0):
            return result
        time.sleep(0.01)
    raise AssertionError(f"Timeout: {result}")


@pytest.fixture
def callbacks(monkeypatch):
    events = []

    async def post(self, url, **kwargs):
        events.append(json.loads(kwargs["content"]))
        return httpx.Response(200, json={"code": 0}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    return events


@pytest.fixture
def client(tmp_path, callbacks):
    settings = Settings(database=str(tmp_path / "tasks.db"), mock_step_seconds=0.01,
                        callback_retry_seconds=0.01)
    with TestClient(create_app(settings)) as instance:
        yield instance


def initialize(client):
    assert client.post("/api/v1/robot/initialize", json=dict(taskId="init", callbackUrl=CALLBACK)).json()["code"] == 0
    assert wait_for(client, "init")["status"] == "SUCCEEDED"


def test_four_tasks_and_ordered_callbacks(client, callbacks):
    initialize(client)
    cases = [("NWS_CYCLE", dict(count=3), 3),
             ("NLG_WOW_OPERATION", dict(wowSignal="AIR"), 1),
             ("GEAR_OPERATION", dict(count=2, preAction="NONE", cycleMode="FINAL_RETRACT_ONLY"), 2)]
    for kind, params, total in cases:
        response = client.post("/api/v1/tasks", json=payload(kind, kind, params))
        assert response.json()["status"] == "ACCEPTED"
        result = wait_for(client, kind, delivered=True)
        assert result["status"] == "SUCCEEDED"
        assert result["progress"] == total
        events = [e for e in callbacks if e["taskId"] == kind]
        assert events[0]["status"] == "ACCEPTED" and events[-1]["status"] == "SUCCEEDED"
        assert all(e["status"] == "RUNNING" for e in events[1:-1])
        progress = [e["progress"] for e in events]
        assert progress == sorted(progress) and set(progress) == set(range(total + 1))
        assert all(e["errorCode"] is None and "+00:00" in e["timestamp"] for e in events)


@pytest.mark.parametrize("kind,params", [
    ("NWS_CYCLE", {"count": 0}),
    ("NWS_CYCLE", {"count": True}),
    ("NWS_CYCLE", {"count": "20"}),
    ("NWS_CYCLE", {"direction": "LEFT", "count": 2}),
    ("NLG_WOW_OPERATION", {}), ("NLG_WOW_OPERATION", {"wowSignal": "ON"}),
    ("GEAR_OPERATION", {"action": "UP"}), ("UNKNOWN", {}),
    ("ROBOT_INITIALIZE", {}), ("PEDAL_ROTATE", {"direction": "LEFT", "count": 1}),
    ("MLG_WOW", {"wowState": "ON"}),
    ("GEAR_OPERATION", {"count": 1}),
    ("GEAR_OPERATION", {"count": 0, "preAction": "NONE", "cycleMode": "FULL_CYCLE"}),
    ("GEAR_OPERATION", {"count": 1, "preAction": "NONE", "cycleMode": "UNKNOWN"}),
])
def test_invalid_parameters(client, kind, params):
    response = client.post("/api/v1/tasks", json=payload("invalid", kind, params))
    assert response.status_code == 400
    assert response.json()["code"] == 1001


def test_initialization_gate_duplicate_unknown_and_terminal_immutable(client):
    request = payload("nws", "NWS_CYCLE", dict(count=2))
    assert client.post("/api/v1/tasks", json=request).json()["code"] == 2003
    initialize(client)
    assert client.post("/api/v1/robot/initialize", json=dict(taskId="init", callbackUrl=CALLBACK)).json()["code"] == 1003
    before = client.post("/api/v1/tasks/init/query").json()
    client.post("/api/v1/tasks/init/cancel", json={})
    assert client.post("/api/v1/tasks/init/query").json()["timestamp"] == before["timestamp"]
    assert client.post("/api/v1/tasks/missing/query").json()["code"] == 1001
    assert client.post("/api/v1/tasks/missing/cancel", json={}).json()["code"] == 1001


def test_busy_concurrent_submissions_and_controlled_cancel(client):
    initialize(client)
    def submit(i):
        return client.post("/api/v1/tasks", json=payload(f"nws-{i}", "NWS_CYCLE",
            dict(count=100))).json()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(submit, range(4)))
    accepted = [r for r in results if r["code"] == 0]
    assert len(accepted) == 1
    assert sum(r["code"] == 2001 for r in results) == 3
    task_id = accepted[0]["taskId"]
    response = client.post(f"/api/v1/tasks/{task_id}/cancel", json={"reason": "test"}).json()
    assert response["status"] in {"ACCEPTED", "RUNNING"}
    result = wait_for(client, task_id)
    assert result["status"] == "CANCELLED"
    assert result["progress"] < 100
    assert result["cancelRequested"] is True


def test_initialize_auth_and_required_fields(tmp_path, callbacks):
    settings = Settings(database=str(tmp_path / "auth.db"), api_key="test-key",
                        callback_url=CALLBACK, mock_step_seconds=0.001)
    with TestClient(create_app(settings)) as client:
        assert client.post("/api/v1/robot/initialize", json={}).json()["code"] == 1002
        client.headers["X-API-Key"] = "test-key"
        assert client.post("/api/v1/robot/initialize", json={}).json()["code"] == 1001
        assert client.post("/api/v1/robot/initialize", json={"taskId": "id"}).json()["code"] == 1001
        response = client.post("/api/v1/robot/initialize", json={"taskId": "provided", "callbackUrl": CALLBACK}).json()
        assert response["taskId"] == "provided"
        assert wait_for(client, "provided")["progress"] == 1


def test_initialize_missing_callback(client):
    assert client.post("/api/v1/robot/initialize", json={"taskId": "id"}).json()["code"] == 1001


def test_retry_exact_payload_and_no_action_replay(tmp_path, monkeypatch):
    attempts = []

    async def post(self, url, **kwargs):
        attempts.append(kwargs["content"])
        return httpx.Response(200, json={"code": 9}, request=httpx.Request("POST", url))

    class CountingAdapter(MockControlAdapter):
        calls = 0

        async def initialize(self):
            self.calls += 1

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    adapter = CountingAdapter()
    settings = Settings(database=str(tmp_path / "retry.db"), callback_retry_seconds=0.001)
    with TestClient(create_app(settings, LocalExecutor(adapter))) as client:
        client.post("/api/v1/robot/initialize", json=dict(taskId="retry", callbackUrl=CALLBACK))
        result = wait_for(client, "retry", delivered=True)
        assert result["status"] == "SUCCEEDED"
        assert result["callbackDelivery"]["exhausted"] == 5
        assert adapter.calls == 1
        assert len(attempts) == 20
        assert attempts[:4] == [attempts[0]] * 4


def test_retry_recovers_after_transport_failure(tmp_path, monkeypatch):
    attempts = []

    async def post(self, url, **kwargs):
        attempts.append(kwargs["content"])
        if len(attempts) == 1:
            raise httpx.ConnectError("offline")
        return httpx.Response(200, json={"code": 0}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    settings = Settings(database=str(tmp_path / "retry.db"), mock_step_seconds=0.001, callback_retry_seconds=0.001)
    with TestClient(create_app(settings)) as client:
        initialize(client)
        result = wait_for(client, "init", delivered=True)
        assert result["callbackDelivery"] == dict(delivered=5, pending=0, exhausted=0)
        assert attempts[0] == attempts[1]


def test_control_failure_preserves_progress_and_requires_initialize(tmp_path, callbacks):
    class BrokenAdapter(MockControlAdapter):
        calls = 0
        stopped = False

        async def nws_rotate(self, direction):
            self.calls += 1
            if self.calls == 4:
                raise RuntimeError("controller unavailable")

        async def safe_stop(self):
            self.stopped = True

    adapter = BrokenAdapter(0.001)
    with TestClient(create_app(Settings(database=str(tmp_path / "failed.db")), LocalExecutor(adapter))) as client:
        initialize(client)
        request = payload("failed", "NWS_CYCLE", dict(count=3))
        client.post("/api/v1/tasks", json=request)
        result = wait_for(client, "failed")
        assert (result["status"], result["progress"], result["errorCode"]) == ("FAILED", 1, "2004")
        assert adapter.stopped
        request["taskId"] = "next"
        assert client.post("/api/v1/tasks", json=request).json()["code"] == 2003


def test_restart_fails_unknown_task_without_replaying_and_keeps_ids(tmp_path, callbacks):
    database = str(tmp_path / "restart.db")
    store = Store(database)
    request = TaskRequest.model_validate(payload("interrupted", "NWS_CYCLE", dict(count=20)))
    store.insert(request)
    store.transition(request.taskId, "RUNNING", 3, "completed 3")
    store.close()
    with TestClient(create_app(Settings(database=database))) as client:
        result = client.post("/api/v1/tasks/interrupted/query").json()
        assert (result["status"], result["progress"], result["errorCode"]) == ("FAILED", 3, "2004")
        assert client.post("/api/v1/tasks", json=request.model_dump(mode="json")).json()["code"] == 1003
        assert client.get("/health").json()["initialized"] is False


def test_cancel_waits_for_safe_stop():
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()

        class Adapter(MockControlAdapter):
            async def safe_stop(self):
                started.set()
                await release.wait()

        async def report(progress, message):
            pass

        cancel = asyncio.Event()
        cancel.set()
        task = asyncio.create_task(LocalExecutor(Adapter()).execute(
            TaskRequest.model_validate(payload("cancel")), cancel, report))
        await started.wait()
        assert not task.done()
        release.set()
        assert (await task).status == "CANCELLED"
    asyncio.run(scenario())


def test_callback_receiver_deduplicates_by_timestamp(tmp_path):
    with TestClient(callback_app(str(tmp_path / "eln.db"))) as client:
        event = dict(taskId="task", taskType="NWS_CYCLE", status="RUNNING", progress=1,
                     message="completed 1", timestamp="2026-09-17T10:00:00+08:00", errorCode=None)
        path = "/eln/api/v1/robot-callbacks/task-status"
        assert client.post(path, json=event).json()["code"] == 0
        client.post(path, json=event)
        event.update(progress=2)
        client.post(path, json=event)
        events = client.post("/eln/api/v1/tasks/task/events").json()["events"]
        assert [e["progress"] for e in events] == [1, 2]
