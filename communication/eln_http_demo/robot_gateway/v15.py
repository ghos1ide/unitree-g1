"""V1.5 request contract and HTTP-only simulated stage execution."""

import asyncio
from typing import Literal

from pydantic import HttpUrl, Field, StrictInt, model_validator
from typing_extensions import Annotated

from .adapters import Result
from .models import StrictModel, TaskId, InitializeParameters

Count = Annotated[StrictInt, Field(gt=0)]
ControlType = Literal["NWS_CYCLE", "NLG_WOW_OPERATION", "GEAR_OPERATION"]


class NwsParameters(StrictModel):
    count: Count


class WowParameters(StrictModel):
    wowSignal: Literal["AIR", "GROUND"]


class GearParameters(StrictModel):
    count: Count
    preAction: Literal["NONE", "EXTEND_DOWNLOCK"]
    cycleMode: Literal["FULL_CYCLE", "FINAL_RETRACT_ONLY"]


class CallbackRequest(StrictModel):
    taskId: TaskId
    callbackUrl: HttpUrl

    @model_validator(mode="after")
    def validate_callback(self):
        if self.callbackUrl.username or self.callbackUrl.password or self.callbackUrl.fragment:
            raise ValueError("callbackUrl must not include credentials or a fragment")
        return self


class TaskRequest(CallbackRequest):
    taskType: Literal["ROBOT_INITIALIZE", "NWS_CYCLE", "NLG_WOW_OPERATION", "GEAR_OPERATION"]
    parameters: dict = Field(json_schema_extra={"examples": [
        {"count": 20}, {"wowSignal": "AIR"},
        {"count": 5, "preAction": "NONE", "cycleMode": "FULL_CYCLE"}]})

    @model_validator(mode="after")
    def validate_parameters(self):
        model = {"ROBOT_INITIALIZE": InitializeParameters, "NWS_CYCLE": NwsParameters,
                 "NLG_WOW_OPERATION": WowParameters, "GEAR_OPERATION": GearParameters}[self.taskType]
        self.parameters = model.model_validate(self.parameters).model_dump()
        return self

    @property
    def total(self):
        return self.parameters["count"] if self.taskType in ("NWS_CYCLE", "GEAR_OPERATION") else 1


class ControlRequest(TaskRequest):
    taskType: ControlType


class SimulationExecutor:
    def __init__(self, step_seconds):
        self.step_seconds = step_seconds

    async def stage(self, name, cancel):
        """A completed delay simulates a stage confirmation, never a hardware read."""
        if cancel.is_set():
            return False
        try:
            await asyncio.wait_for(cancel.wait(), timeout=self.step_seconds)
            return False
        except asyncio.TimeoutError:
            return True

    async def execute(self, request, cancel, report):
        progress = 0
        params = request.parameters
        try:
            if request.taskType == "GEAR_OPERATION" and params["preAction"] == "EXTEND_DOWNLOCK":
                if not await self.stage("PRE_EXTEND_DOWNLOCK", cancel):
                    return Result("CANCELLED", 0, "Simulation cancelled during preAction")
            for index in range(request.total):
                if request.taskType == "NWS_CYCLE":
                    stages = ["NWS_LEFT", "NWS_RIGHT"]
                elif request.taskType == "GEAR_OPERATION":
                    stages = ["RETRACT_UPLOCK"]
                    if params["cycleMode"] == "FULL_CYCLE" or index < request.total - 1:
                        stages.append("EXTEND_DOWNLOCK")
                elif request.taskType == "NLG_WOW_OPERATION":
                    stages = ["WOW_" + params["wowSignal"]]
                else:
                    stages = ["INITIALIZE"]
                for stage in stages:
                    if not await self.stage(stage, cancel):
                        return Result("CANCELLED", progress, "Simulation cancelled during " + stage)
                progress += 1
                await report(progress, f"Simulated {request.taskType}: completed {progress}/{request.total}")
            return Result("SUCCEEDED", progress, f"Simulation completed {progress}/{request.total}")
        except Exception:
            return Result("FAILED", progress, "Simulated executor failed", "2004")

    async def close(self):
        pass
