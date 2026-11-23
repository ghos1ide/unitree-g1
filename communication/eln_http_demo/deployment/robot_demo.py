"""Dedicated HTTP-only demo service; never loads a hardware controller."""
import json
from pathlib import Path

import uvicorn

from robot_gateway.app import create_app
from robot_gateway.config import Settings


def main(backend="mock"):
    root = Path(__file__).resolve().parents[1]
    config = json.loads((root / "data" / "robot_connection.json").read_text(encoding="utf-8"))
    settings = Settings(database=str(root / "data" / "gateway.db"), backend=backend,
                        api_key=config["api_key"], mock_step_seconds=0.5, simulation=True,
                        action_name="/eln_demo/robot_task")
    uvicorn.run(create_app(settings), host="0.0.0.0", port=18000, workers=1)


if __name__ == "__main__":
    main()
