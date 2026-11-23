# Offline trajectory replay (2026-11-19)

The run covers `action_runtime_v3/config/selected_bundle.json` and both
versions in `action_runtime_v3/data/action_releases`. See [summary.json](summary.json)
for input hashes, counts, interpolation errors, and dry-run outcomes.

All 15 version/action pairs passed. The 115,424 sample rows include 33,298
archived arm knots. A NumPy matrix-multiplication warning was logged during
runtime join planning. The separate [planner check](planner_check.json) found
finite results in 1,400 comparisons against an explicit sum of products and
reproduced all seven generated joins exactly.

Each `<version>/<action>/` contains:

- `trajectory.png`: joint position, velocity, and acceleration of the archived
  path. Numbers mark stages; blue shades mark reference alignment, amber marks
  arm holds during hand commands.
- `samples.csv`: the current gateway interpolator sampled at 10 ms plus every
  original arm knot and each stage endpoint. Times are integer nanoseconds;
  neighboring stages include both sides of shared boundaries.
- `trajectory.json`: original plan segments, per-stage hashes and errors, and
  a separate runtime sequence dry-run from the release's initialization target.
  Runtime-generated joins are stored separately and do not replace the archived
  reference. Hand targets are commands, not finger measurements; placeholder
  arm poses stored in hand commands are not applied to the replay curve.

The numeric replay compiled only a standalone C++ sampler, using the v3
gateway's interpolation function verbatim and the existing offline harness.
The sequence dry-run used `OfflineTransport` and the matched planner URDF.
No ROS node, DDS, HTTP service, hardware client, or robot command was used.
These results do not validate collision clearance, grasp, contact, external
signals, or real robot timing.

To reproduce from the workspace with the local Python environment, ROS message
definitions (no node required), NumPy, SciPy, Matplotlib and `g++`:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 execution/action_replay_offline_20261119/replay.py \
  --output execution/action_replay_offline_20261119_rerun
```
