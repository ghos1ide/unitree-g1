# ELN boot services

`eln_executor.service` starts the existing native supervisor on loopback port
8795. `eln_interface.service` remains the public entry point on 8090. The
`eln_interface.service.d/executor.conf` drop-in pulls in the backend and waits
for its startup health check before starting the relay.

Startup must report the real backend in DETACHED state, with no initialized
controller or active task. No initialize, recover, or motion request is sent.
Hardware connectivity is reported separately by `/health`; HTTP readiness is
not authorization to move the robot.

## Installation

Installing these files changes system configuration outside the repository and
requires the project owner's approval. Check that no manually started executor
or controller is running before installation. Do not restart these services
during a robot task.

```bash
sudo install -m 0644 deploy/eln_executor.service /etc/systemd/system/eln_executor.service
sudo install -d -m 0755 /etc/systemd/system/eln_interface.service.d
sudo install -m 0644 deploy/eln_interface.service.d/executor.conf /etc/systemd/system/eln_interface.service.d/executor.conf
sudo systemctl daemon-reload
sudo systemctl enable eln_executor.service eln_interface.service
sudo systemctl start eln_executor.service
```

An already running relay recovers as soon as the backend becomes ready; it does
not need to be restarted. Verify `/health` and `/docs` through port 8090 and
repeat verification after the next actual reboot. Do not claim reboot testing
from a successful `systemctl start` alone.

The supervisor handles child shutdown. `KillMode=process` and `SendSIGKILL=no`
deliberately prevent systemd from force-killing a gateway retained for physical
support. Consequently a stopped service can retain controller processes; do
not manually kill them or treat service stop as a physical E-stop. The shared
ownership lock remains visible to manual launches (`PrivateTmp=false`).

Backend child logs are in `execution/action_runtime_v3/data/service_logs/`;
supervisor and readiness messages are in `journalctl -u eln_executor.service`.
