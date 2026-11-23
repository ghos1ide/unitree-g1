#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ "$EUID" -ne 0 ]]; then
    echo "Run with sudo in a local terminal." >&2
    exit 1
fi
if [[ "$root" != /home/unitree/rh56dfx/workspace ]]; then
    echo "Service paths require /home/unitree/rh56dfx/workspace." >&2
    exit 1
fi
if ! systemctl is-active --quiet eln_monitor.service &&
    [[ -n "$(ss -H -ltn 'sport = :8092')" ]]; then
    echo "Port 8092 is occupied. Inspect its owner before installing the monitor." >&2
    exit 1
fi

systemd-analyze verify "$root/deploy/eln_monitor.service"
install -m 0644 "$root/deploy/eln_monitor.service" /etc/systemd/system/eln_monitor.service
systemctl daemon-reload
systemctl enable eln_monitor.service
systemctl restart eln_monitor.service
systemctl is-enabled eln_monitor.service
systemctl --no-pager status eln_monitor.service
