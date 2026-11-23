"""Run on the robot with its existing Unitree Python 3.8 interpreter."""
import json
import os
import signal
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path("/home/unitree/rh56dfx/workspace/communication/eln_http_demo")
PYTHON = Path("/home/unitree/.miniconda3/envs/Unitree/bin/python")


def health():
    try:
        with urllib.request.urlopen("http://127.0.0.1:18000/health", timeout=3) as response:
            return json.load(response)
    except urllib.error.URLError:
        return None


def running(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().split()[2] != "Z"
    except FileNotFoundError:
        return False


def stop_services():
    state = health()
    if state and state.get("activeTaskId"):
        raise RuntimeError("Robot has an active task; finish or cancel it before upgrading")
    processes = []
    for name, module in (("server", "deployment.robot"), ("controller", "robot_control_bridge.fake_action_server")):
        path = ROOT / "data" / (name + ".pid")
        if not path.exists():
            continue
        pid = int(path.read_text().strip())
        if not running(pid):
            continue
        command = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode()
        if Path(f"/proc/{pid}/cwd").resolve() != ROOT.resolve() or module not in command:
            raise RuntimeError(f"PID {pid} does not belong to the expected project service")
        processes.append(pid)
    for pid in processes:
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 30
        while running(pid):
            if time.monotonic() > deadline:
                raise RuntimeError(f"Service {pid} did not stop; no files replaced")
            time.sleep(0.2)


def main():
    if not PYTHON.exists() or not (ROOT / "data" / "robot_connection.json").exists():
        raise RuntimeError("Existing Unitree environment and robot deployment are required")
    archive = ROOT / "data" / "v15_update.tar.gz"
    with tarfile.open(archive) as bundle:
        for member in bundle.getmembers():
            if not member.isfile() or ROOT.resolve() not in (ROOT / member.name).resolve().parents:
                raise RuntimeError("Invalid package member: " + member.name)
    stop_services()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    backup = ROOT / "data" / "backups" / ("before_v15_" + stamp + ".tar.gz")
    backup.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(backup, "w:gz") as bundle:
        for name in ("robot_gateway", "robot_control_bridge", "pc_a_simulator", "deployment",
                     "ros2_ws/src", "tests", "requirements.txt", "README.md"):
            path = ROOT / name
            if path.exists():
                bundle.add(path, arcname=name)
        for path in (ROOT / "data").iterdir():
            if path.is_file() and path.suffix in {".db", ".sqlite3", ".json", ".log"}:
                bundle.add(path, arcname="data/" + path.name)
    print("Backup:", backup, flush=True)
    with tarfile.open(archive) as bundle:
        bundle.extractall(ROOT)
    subprocess.run([str(PYTHON), "-m", "pip", "install", "-r", "requirements.txt",
                    "--index-url", "https://pypi.tuna.tsinghua.edu.cn/simple"], cwd=ROOT, check=True)
    workspace = ROOT / "ros2_ws"
    previous = workspace / ("previous_" + stamp)
    previous.mkdir()
    for name in ("build", "install", "log"):
        if (workspace / name).exists():
            (workspace / name).rename(previous / name)
    subprocess.run(["bash", "-c", "source /opt/ros/foxy/setup.bash && "
                    "colcon build --packages-select robot_interfaces "
                    "--cmake-args -DPYTHON_EXECUTABLE=/usr/bin/python3"], cwd=workspace, check=True)
    subprocess.run([str(PYTHON), "-m", "pytest", "-q", "tests/test_gateway.py",
                    "tests/test_v15_execution.py"], cwd=ROOT, check=True)
    for name, script in (("controller", "start_fake_controller.sh"), ("server", "start_ros2_gateway.sh")):
        with (ROOT / "data" / (name + ".log")).open("a") as log:
            process = subprocess.Popen(["bash", str(ROOT / "deployment" / script)], cwd=ROOT,
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        (ROOT / "data" / (name + ".pid")).write_text(str(process.pid))
    for _ in range(40):
        state = health()
        if state and state.get("protocolVersion") == "1.5" and state.get("backend") == "ros2":
            print(json.dumps(state), flush=True)
            return
        time.sleep(0.5)
    raise RuntimeError("Services did not become ready; inspect data/server.log and data/controller.log")


if __name__ == "__main__":
    main()
