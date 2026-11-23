import asyncio
import json
import sqlite3
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from deployment.interface_service import create_interface_app
from pc_a_simulator.callback_server import create_app as callback_app
from robot_gateway.app import create_app
from robot_gateway.config import Settings
from robot_gateway.models import TaskRequest as LegacyRequest
from robot_gateway.store import Store
from robot_gateway.v15 import SimulationExecutor, TaskRequest

URL = "http://eln.test/callback"


def request(kind, parameters):
    return TaskRequest(taskId="test", taskType=kind, callbackUrl=URL, parameters=parameters)


class RecordingExecutor(SimulationExecutor):
    def __init__(self, stop_at=None, fail_at=None):
        super().__init__(0.001)
        self.stages = []
        self.stop_at, self.fail_at = stop_at, fail_at

    async def stage(self, name, cancel):
        self.stages.append(name)
        if len(self.stages) == self.fail_at:
            raise RuntimeError("simulated fault")
        if len(self.stages) == self.stop_at:
            cancel.set()
            return False
        return True


def execute(executor, task):
    progress = []
    async def report(count, message):
        progress.append(count)
    async def run():
        return await executor.execute(task, asyncio.Event(), report)
    result = asyncio.run(run())
    return result, progress


@pytest.mark.parametrize("count", [1, 3])
@pytest.mark.parametrize("pre", ["NONE", "EXTEND_DOWNLOCK"])
@pytest.mark.parametrize("mode", ["FULL_CYCLE", "FINAL_RETRACT_ONLY"])
def test_gear_stages_and_count(count, pre, mode):
    executor = RecordingExecutor()
    result, progress = execute(executor, request("GEAR_OPERATION", dict(count=count, preAction=pre, cycleMode=mode)))
    expected = ["PRE_EXTEND_DOWNLOCK"] if pre != "NONE" else []
    expected += ["RETRACT_UPLOCK", "EXTEND_DOWNLOCK"] * (count - 1)
    expected += ["RETRACT_UPLOCK"]
    if mode == "FULL_CYCLE":
        expected += ["EXTEND_DOWNLOCK"]
    assert executor.stages == expected
    assert result.status == "SUCCEEDED" and result.progress == count
    assert progress == list(range(1, count + 1))


def test_nws_requires_two_directions():
    executor = RecordingExecutor()
    result, progress = execute(executor, request("NWS_CYCLE", {"count": 3}))
    assert executor.stages == ["NWS_LEFT", "NWS_RIGHT"] * 3
    assert progress == [1, 2, 3] and result.progress == 3


@pytest.mark.parametrize("signal", ["AIR", "GROUND"])
def test_wow_target(signal):
    executor = RecordingExecutor()
    result, progress = execute(executor, request("NLG_WOW_OPERATION", {"wowSignal": signal}))
    assert executor.stages == ["WOW_" + signal]
    assert progress == [1] and result.status == "SUCCEEDED"


@pytest.mark.parametrize("failure", [False, True])
@pytest.mark.parametrize("kind,params,stop_at,completed", [
    ("NWS_CYCLE", {"count": 3}, 2, 0),
    ("NWS_CYCLE", {"count": 3}, 4, 1),
    ("GEAR_OPERATION", dict(count=2, preAction="EXTEND_DOWNLOCK", cycleMode="FULL_CYCLE"), 1, 0),
    ("GEAR_OPERATION", dict(count=2, preAction="NONE", cycleMode="FULL_CYCLE"), 4, 1),
    ("GEAR_OPERATION", dict(count=1, preAction="NONE", cycleMode="FINAL_RETRACT_ONLY"), 1, 0),
    ("NLG_WOW_OPERATION", {"wowSignal": "AIR"}, 1, 0),
])
def test_partial_stage_not_counted(kind, params, stop_at, completed, failure):
    executor = RecordingExecutor(fail_at=stop_at) if failure else RecordingExecutor(stop_at=stop_at)
    result, progress = execute(executor, request(kind, params))
    assert result.status == ("FAILED" if failure else "CANCELLED")
    assert result.error_code == ("2004" if failure else None)
    assert result.progress == completed
    assert progress == list(range(1, completed + 1))
    assert len(executor.stages) == stop_at


def test_cancel_interrupts_simulated_stage():
    async def run():
        executor = SimulationExecutor(10)
        cancel = asyncio.Event()
        task = asyncio.create_task(executor.stage("NWS_LEFT", cancel))
        await asyncio.sleep(0)
        cancel.set()
        assert await asyncio.wait_for(task, 0.5) is False
    asyncio.run(run())


@pytest.fixture
def client(tmp_path, monkeypatch):
    async def post(self, url, **kwargs):
        return httpx.Response(200, json={"code": 0}, request=httpx.Request("POST", url))
    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    with TestClient(create_interface_app(Settings(database=str(tmp_path / "tasks.db"), mock_step_seconds=0.001))) as client:
        yield client


def test_v15_routes_and_codes(client):
    payload = request("NWS_CYCLE", {"count": 2}).model_dump(mode="json")
    assert client.post("/api/v1/tasks", json=payload).json()["code"] == 2003
    response = client.post("/api/v1/tasks/missing/query")
    assert response.status_code == 404 and response.json()["code"] == 2005
    for body in [{}, {"taskId": "init"}, {"callbackUrl": URL}]:
        assert client.post("/api/v1/robot/initialize", json=body).json()["code"] == 1001
    assert client.post("/api/v1/tasks", json=request("ROBOT_INITIALIZE", {}).model_dump(mode="json")).json()["code"] == 1001
    schema = client.get("/openapi.json").json()
    assert schema["info"]["version"] == "1.5.0"
    assert schema["components"]["schemas"]["ControlRequest"]["properties"]["taskType"]["enum"] == ["NWS_CYCLE", "NLG_WOW_OPERATION", "GEAR_OPERATION"]


@pytest.mark.parametrize("kind,params", [
    ("PEDAL_ROTATE", {"direction": "LEFT", "count": 3}),
    ("MLG_WOW", {"wowState": "ON"}),
    ("GEAR_OPERATION", {"action": "RETRACT"}),
    ("GEAR_OPERATION", {"count": 2}),
    ("GEAR_OPERATION", dict(count=1, preAction="BAD", cycleMode="FULL_CYCLE")),
    ("GEAR_OPERATION", dict(count=1, preAction="NONE", cycleMode="BAD")),
    ("NWS_CYCLE", {"count": True}), ("NWS_CYCLE", {"count": 0}),
    ("NWS_CYCLE", {"count": 1.5}), ("NWS_CYCLE", {"count": "2"}),
    ("NWS_CYCLE", {"count": 2, "direction": "LEFT"}),
    ("NLG_WOW_OPERATION", {"wowSignal": "ON"}),
])
def test_invalid_v15_parameters(client, kind, params):
    response = client.post("/api/v1/tasks", json=dict(taskId="invalid", taskType=kind, callbackUrl=URL, parameters=params))
    assert response.status_code == 400 and response.json()["code"] == 1001


def test_callback_dedup_includes_progress(tmp_path):
    database = str(tmp_path / "eln.db")
    event = dict(taskId="nws", taskType="NWS_CYCLE", status="RUNNING", progress=1,
                 message="test", timestamp="2026-09-20T12:00:00+08:00", errorCode=None)
    with TestClient(callback_app(database)) as client:
        for progress in [1, 1, 2, 2]:
            assert client.post("/eln/api/v1/robot-callbacks/task-status", json=dict(event, progress=progress)).json()["code"] == 0
        events = client.post("/eln/api/v1/tasks/nws/events").json()["events"]
        assert [e["progress"] for e in events] == [1, 2]


def test_legacy_database_not_reinterpreted(tmp_path):
    path = str(tmp_path / "old.db")
    store = Store(path)
    store.insert(LegacyRequest(taskId="old", taskType="GEAR_OPERATION", callbackUrl=URL, parameters={"action": "RETRACT"}))
    store.close()
    with pytest.raises(ValueError, match="incompatible"):
        Store(path, TaskRequest)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM tasks WHERE id='old'").fetchone()[0] == 1


def test_v15_restart_keeps_progress_and_dedup(tmp_path):
    path = str(tmp_path / "new.db")
    store = Store(path, TaskRequest)
    store.insert(request("NWS_CYCLE", {"count": 3}))
    store.transition("test", "RUNNING", 1, "test")
    store.close()
    store = Store(path, TaskRequest, "9000")
    store.recover()
    state = json.loads(store.get("test")["event"])
    assert state["status"] == "FAILED" and state["progress"] == 1
    assert state["errorCode"] == "9000"
    assert store.transition("test", "SUCCEEDED", 3, "late") == state
    store.close()


def test_v15_failure_callback_and_initialization_reset(tmp_path, monkeypatch):
    events = []
    async def post(self, url, **kwargs):
        events.append(json.loads(kwargs["content"]))
        return httpx.Response(200, json={"code": 0}, request=httpx.Request("POST", url))
    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    executor = RecordingExecutor(fail_at=2)
    settings = Settings(protocol_version="1.5", database=str(tmp_path / "failure.db"))
    with TestClient(create_app(settings, executor)) as client:
        response = client.post("/api/v1/robot/initialize", json=dict(taskId="init", callbackUrl=URL))
        assert response.json()["code"] == 0
        deadline = time.monotonic() + 3
        while not client.get("/health").json()["initialized"]:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        response = client.post("/api/v1/tasks", json=request("NWS_CYCLE", {"count": 2}).model_dump(mode="json"))
        assert response.json()["code"] == 0
        while not any(e["status"] == "FAILED" for e in events):
            assert time.monotonic() < deadline
            time.sleep(0.01)
        failed = next(e for e in events if e["status"] == "FAILED")
        assert failed["errorCode"] == "2004" and failed["progress"] == 0
        assert client.get("/health").json()["initialized"] is False


def test_callback_migrates_legacy_records(tmp_path):
    database = str(tmp_path / "legacy-eln.db")
    event = dict(taskId="old", taskType="NWS_CYCLE", status="RUNNING", progress=1,
                 message="test", timestamp="2026-09-20T12:00:00+08:00", errorCode=None)
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE events(id INTEGER PRIMARY KEY,task_id TEXT,status TEXT,timestamp TEXT,payload TEXT)")
        db.execute("INSERT INTO events VALUES(1,?,?,?,?)", ("old", "RUNNING", event["timestamp"], json.dumps(event)))
    for _ in range(2):
        with TestClient(callback_app(database)) as client:
            assert client.post("/eln/api/v1/tasks/old/events").json()["events"] == [event]
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT count(*) FROM events").fetchone()[0] == 1
