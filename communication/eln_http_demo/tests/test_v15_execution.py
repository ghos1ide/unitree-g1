import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from robot_gateway.adapters import LocalExecutor, MockControlAdapter
from robot_gateway.app import create_app
from robot_gateway.config import Settings
from robot_gateway.models import SimulationConfig, TaskRequest
from robot_gateway.store import Store


class RecordingAdapter(MockControlAdapter):
    def __init__(self):
        super().__init__(0)
        self.actions = []
        self.stopped = False

    async def nws_rotate(self, direction):
        self.actions.append(direction)
        await super().nws_rotate(direction)

    async def gear_operation(self, action):
        self.actions.append(action)
        await super().gear_operation(action)

    async def confirm_lock(self, lock):
        self.actions.append("CONFIRM_" + lock)
        return await super().confirm_lock(lock)

    async def safe_stop(self):
        self.stopped = True
        await super().safe_stop()


def execute(kind, parameters, adapter=None, fault="NONE", occurrence=1, stop_on=None):
    adapter = adapter or RecordingAdapter()
    events = []

    async def run():
        stop = asyncio.Event()
        executor = LocalExecutor(adapter)
        executor.configure_simulation(SimulationConfig(fault=fault, occurrence=occurrence))

        async def report(progress, message):
            events.append((progress, message))
            if stop_on and stop_on in message:
                stop.set()

        request = TaskRequest(taskId="test", taskType=kind, callbackUrl="http://localhost/callback",
                              parameters=parameters)
        result = await executor.execute(request, stop, report)
        return result

    return asyncio.run(run()), adapter, events


def gear_parameters(count=3, pre="NONE", mode="FULL_CYCLE"):
    return dict(count=count, preAction=pre, cycleMode=mode)


def test_nws_counts_pairs_only():
    result, adapter, events = execute("NWS_CYCLE", dict(count=20))
    assert adapter.actions == ["LEFT", "RIGHT"] * 20
    assert result.status == "SUCCEEDED" and result.progress == 20
    for progress, message in events:
        if message.startswith("NWS_RIGHT"):
            assert progress < 20
    assert [n for n, msg in events if msg.startswith("Completed")] == list(range(1, 21))


@pytest.mark.parametrize("count", [1, 3])
@pytest.mark.parametrize("pre", ["NONE", "EXTEND_DOWNLOCK"])
@pytest.mark.parametrize("mode", ["FULL_CYCLE", "FINAL_RETRACT_ONLY"])
def test_gear_exact_sequence_and_final_lock(count, pre, mode):
    result, adapter, events = execute("GEAR_OPERATION", gear_parameters(count, pre, mode))
    cycle = ["RETRACT", "CONFIRM_UP", "EXTEND", "CONFIRM_DOWN"]
    expected = ["EXTEND", "CONFIRM_DOWN"] if pre == "EXTEND_DOWNLOCK" else []
    expected += cycle * (count if mode == "FULL_CYCLE" else count - 1)
    if mode == "FINAL_RETRACT_ONLY":
        expected += ["RETRACT", "CONFIRM_UP"]
    assert adapter.actions == expected
    assert adapter.gear_lock == ("DOWN" if mode == "FULL_CYCLE" else "UP")
    assert result.status == "SUCCEEDED" and result.progress == count
    assert [n for n, msg in events if msg.startswith("Completed")] == list(range(1, count + 1))
    if pre != "NONE":
        assert events[0][0] == events[1][0] == events[2][0] == 0


@pytest.mark.parametrize("signal", ["AIR", "GROUND"])
def test_wow_confirmation(signal):
    result, adapter, events = execute("NLG_WOW_OPERATION", dict(wowSignal=signal))
    assert result.status == "SUCCEEDED" and result.progress == 1
    assert adapter.wow_signal == signal
    assert events[1] == (0, f"CONFIRM_WOW_{signal} (0/1)")


@pytest.mark.parametrize("kind,params,fault,occurrence,progress,code", [
    ("NWS_CYCLE", dict(count=3), "ACTUATOR_FAULT", 2, 0, "2004"),
    ("NWS_CYCLE", dict(count=3), "ACTUATOR_FAULT", 4, 1, "2004"),
    ("NWS_CYCLE", dict(count=3), "SAFETY_INTERLOCK", 2, 0, "2002"),
    ("NLG_WOW_OPERATION", dict(wowSignal="AIR"), "WOW_TIMEOUT", 1, 0, "2004"),
    ("GEAR_OPERATION", gear_parameters(), "UPLOCK_TIMEOUT", 1, 0, "2004"),
    ("GEAR_OPERATION", gear_parameters(), "DOWNLOCK_TIMEOUT", 2, 1, "2004"),
    ("GEAR_OPERATION", gear_parameters(pre="EXTEND_DOWNLOCK"), "DOWNLOCK_TIMEOUT", 1, 0, "2004"),
    ("GEAR_OPERATION", gear_parameters(count=1, mode="FINAL_RETRACT_ONLY"), "UPLOCK_TIMEOUT", 1, 0, "2004"),
])
def test_failure_never_counts_unconfirmed_operation(kind, params, fault, occurrence, progress, code):
    result, adapter, events = execute(kind, params, fault=fault, occurrence=occurrence)
    assert (result.status, result.progress, result.error_code) == ("FAILED", progress, code)
    assert adapter.stopped
    if fault == "UPLOCK_TIMEOUT":
        assert "EXTEND" not in adapter.actions
    if params.get("preAction") == "EXTEND_DOWNLOCK":
        assert "RETRACT" not in adapter.actions


@pytest.mark.parametrize("kind,params,stop_on,progress", [
    ("NWS_CYCLE", dict(count=3), "NWS_LEFT", 0),
    ("NWS_CYCLE", dict(count=3), "NWS_RIGHT", 1),
    ("GEAR_OPERATION", gear_parameters(), "GEAR_RETRACT", 0),
    ("GEAR_OPERATION", gear_parameters(), "CONFIRM_UPLOCK", 0),
    ("GEAR_OPERATION", gear_parameters(pre="EXTEND_DOWNLOCK"), "PREACTION_DOWNLOCK", 0),
    ("GEAR_OPERATION", gear_parameters(count=1, mode="FINAL_RETRACT_ONLY"), "CONFIRM_UPLOCK", 1),
])
def test_cancel_at_stage_boundary(kind, params, stop_on, progress):
    result, adapter, _ = execute(kind, params, stop_on=stop_on)
    assert result.status == "CANCELLED" and result.progress == progress
    assert adapter.stopped
    if stop_on == "NWS_LEFT":
        assert adapter.actions == ["LEFT"]


def test_v10_record_is_preserved_without_reexecution(tmp_path):
    store = Store(str(tmp_path / "old.db"))
    old_request = dict(taskId="old", taskType="PEDAL_ROTATE", callbackUrl="http://localhost/callback",
                       parameters=dict(direction="LEFT", count=20))
    old_event = dict(taskId="old", taskType="PEDAL_ROTATE", status="RUNNING", progress=3,
                     message="running", timestamp="2026-09-17T10:00:00+00:00", errorCode=None)
    with store.db:
        store.db.execute("INSERT INTO tasks(id,request,event,status) VALUES(?,?,?,?)",
                          ("old", json.dumps(old_request), json.dumps(old_event), "RUNNING"))
    store.recover()
    event = json.loads(store.get("old")["event"])
    assert event["status"] == "FAILED" and event["progress"] == 3
    assert event["taskType"] == "PEDAL_ROTATE"
    store.close()


def test_simulation_extension_disabled_for_non_simulation(tmp_path):
    settings = Settings(database=str(tmp_path / "sim.db"), backend="ros2", simulation=False)
    with TestClient(create_app(settings, LocalExecutor(MockControlAdapter(0)))) as client:
        assert client.post("/api/v1/simulation/config", json={"fault": "WOW_TIMEOUT"}).status_code == 400
