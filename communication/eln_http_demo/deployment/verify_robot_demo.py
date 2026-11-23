"""Exercise the real remote HTTP gateway while retaining simulated actions."""
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from pc_a_simulator.remote_demo import RemoteDemoRuntime
from pc_a_simulator.scenarios import SAMPLES


def main():
    runtime = RemoteDemoRuntime(callback_port=18082)
    report = dict(timestamp=datetime.now(timezone.utc).isoformat(), tasks=[])
    try:
        runtime.start()
        report["gateway"] = runtime.gateway_url
        report["health"] = runtime.health()
        runtime.configure_simulation()
        for kind, parameters in SAMPLES:
            received = []
            result = runtime.run_task(kind, parameters, threading.Event(),
                lambda event, data: received.append(data) if event == "event" else None)
            assert result["status"] == "SUCCEEDED", result
            assert result["progress"] == parameters.get("count", 1)
            assert received[0]["status"] == "ACCEPTED"
            assert received[-1]["status"] == "SUCCEEDED"
            report["tasks"].append(dict(taskId=result["taskId"], taskType=kind,
                status=result["status"], progress=result["progress"], callbacks=len(received)))
            print(kind, result["status"], result["progress"], "callbacks", len(received), flush=True)
        stop = threading.Event()

        def request_cancel(event, data):
            if event == "state" and data["progress"] >= 1:
                stop.set()

        result = runtime.run_task("NWS_CYCLE", dict(count=20), stop, request_cancel)
        assert result["status"] == "CANCELLED"
        assert 1 <= result["progress"] < 20
        report["cancel"] = dict(taskId=result["taskId"], status=result["status"], progress=result["progress"])
        response = runtime.client.post(runtime.gateway_url + "/api/v1/tasks", json={
            "taskId": result["taskId"], "taskType": "NWS_CYCLE", "callbackUrl": runtime.callback_url,
            "parameters": {"count": 20}}, headers={"X-API-Key": runtime.api_key})
        assert response.json()["code"] == 1003
        report["duplicate_code"] = 1003
        response = runtime.client.post(runtime.gateway_url + "/api/v1/tasks/unknown/query")
        assert response.status_code == 401
        report["unauthorized_http"] = 401
        print("Cancel, duplicate rejection and API authentication passed", flush=True)
    finally:
        runtime.close()
    output = Path(__file__).resolve().parents[1] / "data" / "robot_verification.json"
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("Report:", output, flush=True)


if __name__ == "__main__":
    main()
