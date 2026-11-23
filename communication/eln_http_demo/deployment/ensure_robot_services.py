"""Start missing demo services without restarting existing processes."""
import json
import subprocess
import time
from pathlib import Path


def main():
    import fcntl
    from .upgrade_robot_v15 import ROOT, health

    with (ROOT / "data" / "startup.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for name, module, script in (
            ("controller", "robot_control_bridge.fake_action_server", "start_fake_controller.sh"),
            ("server", "deployment.robot_ros2_demo", "start_ros2_gateway.sh"),
        ):
            found = False
            for process in Path("/proc").iterdir():
                if not process.name.isdigit():
                    continue
                try:
                    args = (process / "cmdline").read_bytes().split(b"\0")
                    if module.encode() in args and (process / "cwd").resolve() == ROOT:
                        found = True
                        break
                except (OSError, RuntimeError):
                    continue
            if found:
                print(name + " already running", flush=True)
                continue
            if name == "server" and health() is not None:
                raise RuntimeError("Port 18000 is served by another process; no replacement started")
            with (ROOT / "data" / (name + ".log")).open("a") as log:
                child = subprocess.Popen(["bash", str(ROOT / "deployment" / script)], cwd=ROOT,
                    stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            (ROOT / "data" / (name + ".pid")).write_text(str(child.pid))
            print(name + " started", flush=True)
        for _ in range(40):
            state = health()
            if state and state.get("protocolVersion") == "1.5" and state.get("simulation"):
                print(json.dumps(state), flush=True)
                return
            time.sleep(0.5)
        raise RuntimeError("Demo startup failed; inspect data/server.log and data/controller.log")


if __name__ == "__main__":
    main()
