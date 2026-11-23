from typing import Literal, Optional
from typing_extensions import Annotated

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, StrictInt, StringConstraints, model_validator

TaskId = Annotated[str, StringConstraints(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")]
TaskType = Literal["ROBOT_INITIALIZE", "NWS_CYCLE", "NLG_WOW_OPERATION", "GEAR_OPERATION"]
Status = Literal["ACCEPTED", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED"]
TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED"}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InitializeParameters(StrictModel):
    pass


class CycleParameters(StrictModel):
    count: Annotated[StrictInt, Field(gt=0, le=2147483647)]


class WowParameters(StrictModel):
    wowSignal: Literal["AIR", "GROUND"]


class GearParameters(CycleParameters):
    preAction: Literal["NONE", "EXTEND_DOWNLOCK"]
    cycleMode: Literal["FULL_CYCLE", "FINAL_RETRACT_ONLY"]


PARAMETERS = {
    "ROBOT_INITIALIZE": InitializeParameters,
    "NWS_CYCLE": CycleParameters,
    "NLG_WOW_OPERATION": WowParameters,
    "GEAR_OPERATION": GearParameters,
}


class TaskRequest(StrictModel):
    taskId: TaskId
    taskType: TaskType
    callbackUrl: HttpUrl
    parameters: dict

    @model_validator(mode="after")
    def validate_parameters(self):
        self.parameters = PARAMETERS[self.taskType].model_validate(self.parameters).model_dump()
        if self.callbackUrl.username or self.callbackUrl.password or self.callbackUrl.fragment:
            raise ValueError("callbackUrl must not include credentials or a fragment")
        return self

    @property
    def total(self):
        return self.parameters.get("count", 1)


class InitializeRequest(StrictModel):
    taskId: TaskId
    callbackUrl: HttpUrl


class ControlRequest(TaskRequest):
    taskType: Literal["NWS_CYCLE", "NLG_WOW_OPERATION", "GEAR_OPERATION"]


class SimulationConfig(StrictModel):
    fault: Literal["NONE", "SAFETY_INTERLOCK", "WOW_TIMEOUT", "UPLOCK_TIMEOUT",
                   "DOWNLOCK_TIMEOUT", "ACTUATOR_FAULT"] = "NONE"
    occurrence: Annotated[StrictInt, Field(ge=1, le=2147483647)] = 1


class CancelRequest(StrictModel):
    reason: Annotated[str, Field(max_length=500)] = "ELN operator cancelled"


class TaskEvent(StrictModel):
    taskId: TaskId
    taskType: TaskType
    status: Status
    progress: Annotated[StrictInt, Field(ge=0)]
    message: str
    timestamp: str
    errorCode: Optional[str]
