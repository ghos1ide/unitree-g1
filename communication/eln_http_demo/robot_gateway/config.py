import os
from dataclasses import dataclass
from typing import Optional


@dataclass
class Settings:
    database: str = "data/gateway.sqlite3"
    backend: str = "mock"
    callback_url: Optional[str] = None
    api_key: Optional[str] = None
    simulation: bool = False
    mock_step_seconds: float = 0.5
    callback_timeout: float = 3.0
    callback_retry_seconds: float = 1.0
    reliable_callbacks: bool = False
    action_timeout: float = 300.0
    action_name: str = "/robot_task"

    @classmethod
    def from_env(cls):
        return cls(
            database=os.getenv("ROBOT_DATABASE", "data/gateway.sqlite3"),
            backend=os.getenv("ROBOT_BACKEND", "mock"),
            callback_url=os.getenv("ELN_CALLBACK_URL") or None,
            api_key=os.getenv("ROBOT_API_KEY") or None,
            mock_step_seconds=float(os.getenv("MOCK_STEP_SECONDS", "0.5")),
            callback_timeout=float(os.getenv("CALLBACK_TIMEOUT", "3")),
            callback_retry_seconds=float(os.getenv("CALLBACK_RETRY_SECONDS", "1")),
            action_timeout=float(os.getenv("ACTION_TIMEOUT", "300")),
            action_name=os.getenv("ROS_ACTION_NAME", "/robot_task"),
        )
