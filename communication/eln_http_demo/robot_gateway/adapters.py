import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional, Protocol

from .models import SimulationConfig, TaskRequest

Report = Callable[[int, str], Awaitable[None]]


@dataclass
class Result:
    status: str
    progress: int
    message: str
    error_code: Optional[str] = None


class ControlFault(Exception):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


class RobotControlAdapter(Protocol):
    async def check_safety(self): ...
    async def initialize(self): ...
    async def nws_rotate(self, direction: str): ...
    async def set_nlg_wow(self, signal: str): ...
    async def confirm_wow(self, signal: str) -> bool: ...
    async def gear_operation(self, action: str): ...
    async def confirm_lock(self, lock: str) -> bool: ...
    async def safe_stop(self): ...


class MockControlAdapter:
    """Commands and sensor confirmations are distinct simulated stages."""

    def __init__(self, step_seconds=0.5):
        self.step_seconds = step_seconds
        self.config = SimulationConfig()
        self.wow_signal = None
        self.gear_lock = None
        self.wow_target = None
        self.gear_target = None
        self.hits = {}

    def configure(self, config):
        self.config = config

    def begin_task(self):
        self.hits = {}

    def triggered(self, fault):
        self.hits[fault] = self.hits.get(fault, 0) + 1
        return self.config.fault == fault and self.hits[fault] == self.config.occurrence

    async def check_safety(self):
        if self.triggered("SAFETY_INTERLOCK"):
            raise ControlFault("2002", "Safety interlock active")

    async def motion(self):
        await asyncio.sleep(self.step_seconds)
        if self.triggered("ACTUATOR_FAULT"):
            raise ControlFault("2004", "Simulated actuator fault")

    async def initialize(self):
        await self.motion()
        self.wow_signal = None
        self.gear_lock = None
        self.wow_target = None
        self.gear_target = None

    async def nws_rotate(self, direction):
        await self.motion()

    async def set_nlg_wow(self, signal):
        await self.motion()
        self.wow_target = signal

    async def confirm_wow(self, signal):
        await asyncio.sleep(self.step_seconds)
        if self.triggered("WOW_TIMEOUT") or self.wow_target != signal:
            return False
        self.wow_signal = signal
        return True

    async def gear_operation(self, action):
        self.gear_lock = None
        await self.motion()
        self.gear_target = "UP" if action == "RETRACT" else "DOWN"

    async def confirm_lock(self, lock):
        await asyncio.sleep(self.step_seconds)
        fault = "UPLOCK_TIMEOUT" if lock == "UP" else "DOWNLOCK_TIMEOUT"
        if self.triggered(fault) or self.gear_target != lock:
            return False
        self.gear_lock = lock
        return True

    async def safe_stop(self):
        await asyncio.sleep(min(0.05, self.step_seconds))


class ControlledCancellation(Exception):
    pass


class LocalExecutor:
    def __init__(self, adapter: RobotControlAdapter):
        self.adapter = adapter

    def configure_simulation(self, config):
        if not isinstance(self.adapter, MockControlAdapter):
            raise ValueError("simulation requires a mock adapter")
        self.adapter.configure(config)

    async def execute(self, request: TaskRequest, cancel: asyncio.Event, report: Report):
        progress = 0
        if isinstance(self.adapter, MockControlAdapter):
            self.adapter.begin_task()

        async def checkpoint():
            if cancel.is_set():
                raise ControlledCancellation()
            await self.adapter.check_safety()

        async def stage(message, operation, *args, confirmation=False):
            await checkpoint()
            await report(progress, f"{message} ({progress}/{request.total})")
            result = await operation(*args)
            if confirmation and result is not True:
                raise ControlFault("2004", message + ": target signal not confirmed")

        async def gear(action):
            lock = "UP" if action == "RETRACT" else "DOWN"
            await stage("GEAR_" + action, self.adapter.gear_operation, action)
            await stage("CONFIRM_" + lock + "LOCK", self.adapter.confirm_lock, lock, confirmation=True)

        try:
            p = request.parameters
            if request.taskType == "GEAR_OPERATION" and p["preAction"] == "EXTEND_DOWNLOCK":
                await gear("EXTEND")
                await report(0, "PREACTION_DOWNLOCK_CONFIRMED (0/%s)" % request.total)
            for index in range(request.total):
                if request.taskType == "ROBOT_INITIALIZE":
                    await stage("INITIALIZE", self.adapter.initialize)
                elif request.taskType == "NWS_CYCLE":
                    await stage("NWS_LEFT", self.adapter.nws_rotate, "LEFT")
                    await stage("NWS_RIGHT", self.adapter.nws_rotate, "RIGHT")
                elif request.taskType == "NLG_WOW_OPERATION":
                    signal = p["wowSignal"]
                    await stage("SET_WOW_" + signal, self.adapter.set_nlg_wow, signal)
                    await stage("CONFIRM_WOW_" + signal, self.adapter.confirm_wow, signal, confirmation=True)
                else:
                    await gear("RETRACT")
                    if p["cycleMode"] == "FULL_CYCLE" or index < request.total - 1:
                        await gear("EXTEND")
                # Count only after every required stage of this operation completed.
                progress += 1
                await report(progress, f"Completed {progress}/{request.total}")
            await checkpoint()
            final_message = f"Completed {progress}/{request.total}"
            if request.taskType == "GEAR_OPERATION":
                final_message += "; final lock=" + ("DOWN" if p["cycleMode"] == "FULL_CYCLE" else "UP")
            elif request.taskType == "NLG_WOW_OPERATION":
                final_message += "; WOW=" + p["wowSignal"]
            return Result("SUCCEEDED", progress, final_message)
        except ControlledCancellation:
            await self.adapter.safe_stop()
            return Result("CANCELLED", progress, "Controlled stop completed")
        except ControlFault as exc:
            await self.adapter.safe_stop()
            return Result("FAILED", progress, str(exc), exc.code)
        except Exception:
            await self.adapter.safe_stop()
            raise

    async def close(self):
        pass
