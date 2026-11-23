#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ "$EUID" -ne 0 ]]; then
    echo "Run this installer with sudo in a local terminal." >&2
    exit 1
fi
if [[ "$root" != /home/unitree/rh56dfx/workspace ]]; then
    echo "Service paths require /home/unitree/rh56dfx/workspace." >&2
    exit 1
fi
if ! systemctl is-active --quiet eln_executor.service; then
    if pgrep -f 'standalone_supervisor.py|native_worker.py|arm_sdk_hardware_gateway|production_api.py' >/dev/null; then
        echo "Existing executor/controller detected; inspect ownership before installation." >&2
        exit 1
    fi
fi

install -m 0644 "$root/deploy/eln_executor.service" /etc/systemd/system/eln_executor.service
install -d -m 0755 /etc/systemd/system/eln_interface.service.d
install -m 0644 "$root/deploy/eln_interface.service.d/executor.conf" /etc/systemd/system/eln_interface.service.d/executor.conf
systemctl daemon-reload
systemctl enable eln_executor.service eln_interface.service
systemctl start eln_executor.service
systemctl start eln_interface.service
systemctl --no-pager status eln_executor.service eln_interface.service
