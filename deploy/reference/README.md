# Source deployment references

These are uninstalled copies for review. The `.service` files, ELN interface
drop-in and installers refer to the original robot's software, device ownership
and environment. The installers can modify system configuration and start
hardware services; do not run them from this migrated workspace.

`action_runtime_v3-interface_relay.json` is the old relay configuration, not a
live setting. It was kept out of `execution/action_runtime_v3/config/` so the
migrated interface defaults to simulation. Review authentication and configure
fresh private credentials before enabling any production relay.
