"""Package or upgrade the existing robot demo without creating environments."""
import argparse
import json
import subprocess
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REMOTE = "/home/unitree/rh56dfx/workspace/communication/eln_http_demo"
SSH = r"C:\Windows\System32\OpenSSH\ssh.exe"
SCP = r"C:\Windows\System32\OpenSSH\scp.exe"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--package-only", action="store_true")
    args = parser.parse_args()
    archive = ROOT / "data" / "v15_update.tar.gz"
    archive.parent.mkdir(exist_ok=True)
    with tarfile.open(archive, "w:gz") as bundle:
        for directory in ("robot_gateway", "robot_control_bridge", "pc_a_simulator", "deployment", "tests", "ros2_ws/src", "docs"):
            for path in (ROOT / directory).rglob("*"):
                if path.is_file() and "__pycache__" not in path.parts:
                    bundle.add(path, arcname=path.relative_to(ROOT).as_posix())
        for name in ("requirements.txt", "README.md", "快速开始.md"):
            bundle.add(ROOT / name, arcname=name)
    print("V1.5 package:", archive, flush=True)
    if args.package_only:
        return
    config = json.loads((ROOT / "data" / "robot_connection.json").read_text(encoding="utf-8"))
    target = config["ssh_target"]
    for source, destination in ((archive, "data/v15_update.tar.gz"),
                                 (ROOT / "deployment" / "upgrade_robot_v15.py", "data/upgrade_v15.py")):
        subprocess.run([SCP, "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", str(source),
                        f"{target}:{REMOTE}/{destination}"], check=True, timeout=60)
    subprocess.run([SSH, "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", target,
        f"/home/unitree/.miniconda3/envs/Unitree/bin/python {REMOTE}/data/upgrade_v15.py"],
        check=True, timeout=600)


if __name__ == "__main__":
    main()
