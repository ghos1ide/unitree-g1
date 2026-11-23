# Restore the Python environments

Use `/home/unitree/rh56dfx/workspace` as the working directory. The dependency
bundle under `/home/unitree/unitree-migration/migration-dependencies` supplies
manifests, not packages. Verify it with `sha256sum -c SHA256SUMS` from the
bundle directory. Do not install either `base/` or `system-python/` wholesale,
and leave ROS Foxy's existing Python environment unchanged.

## ELN V1 and V1.5

Both use `communication/eln_http_demo/.venv`. This environment can be created
with the target's Python 3.8.10. It is intentionally separate from the restored
Conda Python 3.8.20 environment. The two project requirements determine what
to install; `Unitree/requirements-pip-only.txt` constrains the versions.

```bash
bash scripts/setup_eln_env.sh --prepare-only
bash scripts/setup_eln_env.sh --wheelhouse /path/to/eln-wheelhouse
```

`--prepare-only` creates the isolated interpreter and bootstraps pip without
downloading application packages. For offline installation, the wheelhouse
needs `pip==24.3.1` plus the wheels for both requirements files and all their
dependencies, including the NumPy version used by ROS2 action type support.
Collect them on a connected, compatible Linux ARM64 CPython 3.8 machine with
pip 24.3.1:

```bash
deps=/home/unitree/unitree-migration/migration-dependencies
python3.8 -m pip download --only-binary=:all: --dest /path/to/eln-wheelhouse \
  'pip==24.3.1' -c "$deps/Unitree/requirements-pip-only.txt" \
  -r requirements-eln.txt -r communication/eln_http_demo/requirements.txt
```

Without arguments, `setup_eln_env.sh` uses PyPI instead. The installer checks
the bundle, runs `pip check` and verifies imports. It does not start services.
Once the dependencies and ROS2 workspace are ready, verify the action type
support without starting a node:

```bash
(cd communication/eln_http_demo && source deployment/ros2_env.sh && \
  .venv/bin/python -c 'from rclpy.type_support import check_for_type_support; from robot_interfaces.action import RobotTask; check_for_type_support(RobotTask)')
```

Run the HTTP tests only in an environment that permits local test sockets.

## Unitree RGB-D

The target reports Ubuntu 20.04, aarch64, L4T R35.3.1 and CUDA 11.4. Keep the
installed JetPack and ROS. Rebuild the original application environment into
`.envs/unitree`, using an ARM64 Conda distribution. Put Conda and its cache
inside `.envs/`; do not run `conda init` or overwrite an existing prefix. Use the
verified ARM64 explicit export to restore the exact Conda package versions.

```bash
deps=/home/unitree/unitree-migration/migration-dependencies
export CONDA_PKGS_DIRS="$PWD/.envs/conda-pkgs"
export CONDA_REGISTER_ENVS=false
conda create --prefix "$PWD/.envs/unitree" --no-default-packages \
  --file "$deps/Unitree/conda-explicit-linux-aarch64.txt"
```

On an offline target, use a prepared Conda package cache matching the explicit
export with `conda create --offline`, or supply the exact archives to a local
Conda package source. The manifests alone cannot create the prefix. Install
the pip portion separately from a trusted ARM64/Python 3.8 wheelhouse:

```bash
vision_python="$PWD/.envs/unitree/bin/python"
TMPDIR="$PWD/.envs" PIP_CONFIG_FILE=/dev/null "$vision_python" -IB -m pip \
  --isolated --no-cache-dir --disable-pip-version-check install \
  --no-index --find-links /path/to/vision-wheelhouse --only-binary=:all: \
  -r "$deps/Unitree/requirements-pip-only.txt"
"$vision_python" -IB -m pip check
"$vision_python" -IB -c 'import cv2, numpy, pyrealsense2, torch, torchvision, ultralytics; print(torch.__version__, torch.version.cuda)'
```

The export specifically records the NVIDIA Jetson `torch==2.0.0+nv23.5`
wheel, `torchvision==0.15.1` built from a local checkout, and
`pyrealsense2==2.55.1.6486`. Use the PyTorch URL and SHA-256 recorded in
`Unitree/pip-freeze-all.txt` to identify and verify that wheel. Supply the
original compatible torchvision wheel or rebuild it against that PyTorch;
do not silently replace either with a generic wheel. The freeze file is
provenance only, not an installation requirements file.

`vision_service` defaults to `.envs/unitree/bin/python`; use `--python` to
point it to a different prepared environment. Import checks do not verify
CUDA execution, RealSense streaming or calibration.
