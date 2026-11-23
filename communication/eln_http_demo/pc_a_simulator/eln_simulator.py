import argparse
import time
from uuid import uuid4

import httpx


def main():
    parser = argparse.ArgumentParser(description="Run all four ELN tasks through the HTTP gateway")
    parser.add_argument("--gateway", default="http://127.0.0.1:8000")
    parser.add_argument("--callback", default="http://127.0.0.1:8001/eln/api/v1/robot-callbacks/task-status")
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--count", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args()
    headers = {"X-API-Key": args.api_key} if args.api_key else {}
    with httpx.Client(base_url=args.gateway, headers=headers, timeout=10, trust_env=False) as client:
        tasks = [
            ("ROBOT_INITIALIZE", {}),
            ("NWS_CYCLE", dict(count=args.count)),
            ("NLG_WOW_OPERATION", dict(wowSignal="AIR")),
            ("GEAR_OPERATION", dict(count=args.count, preAction="NONE", cycleMode="FULL_CYCLE")),
        ]
        for task_type, parameters in tasks:
            task_id = f"DEMO-{task_type}-{uuid4().hex[:12]}"
            if task_type == "ROBOT_INITIALIZE":
                response = client.post("/api/v1/robot/initialize", json=dict(taskId=task_id, callbackUrl=args.callback))
            else:
                response = client.post("/api/v1/tasks", json=dict(taskId=task_id, taskType=task_type,
                    callbackUrl=args.callback, parameters=parameters))
            print(response.json(), flush=True)
            response.raise_for_status()
            deadline = time.monotonic() + args.timeout
            while True:
                response = client.post(f"/api/v1/tasks/{task_id}/query")
                response.raise_for_status()
                result = response.json()
                if result["status"] in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                    print(result, flush=True)
                    if result["status"] != "SUCCEEDED":
                        raise SystemExit("Task did not succeed")
                    break
                if time.monotonic() >= deadline:
                    raise SystemExit("Query timed out; task may still be active")
                time.sleep(0.1)


if __name__ == "__main__":
    main()
