#!/usr/bin/env bash
set -Eeuo pipefail

readonly PROJECT_ROOT="/home/unitree/rh56dfx/workspace"
readonly UNIT_SOURCE="$PROJECT_ROOT/deploy/vision_service.service"
readonly UNIT_TARGET="/etc/systemd/system/vision_service.service"
readonly VISION_BINARY="$PROJECT_ROOT/build/src/vision_service"
readonly VISION_PIDFILE="$PROJECT_ROOT/run/vision_service.pid"
readonly VIDEO_CONFIG="/unitree/etc/master_service/service/video_hub_pc4"
readonly BACKUP_DIR="/unitree/var/backups/vision_service"
readonly VIDEO_BACKUP="$BACKUP_DIR/video_hub_pc4"

usage() {
    echo "Usage: sudo $0 {install|restore|status}"
}

require_root() {
    if [[ $(id -u) -ne 0 ]]; then
        echo "This operation must run as root." >&2
        exit 1
    fi
}

stop_manual_vision_service() {
    [[ -r "$VISION_PIDFILE" ]] || return 0

    local pid executable
    read -r pid < "$VISION_PIDFILE"
    [[ "$pid" =~ ^[0-9]+$ && -e "/proc/$pid/exe" ]] || return 0
    executable=$(readlink -f "/proc/$pid/exe")
    if [[ "$executable" != "$VISION_BINARY" ]]; then
        echo "Refusing to stop PID $pid: executable is $executable" >&2
        exit 1
    fi

    kill -TERM "$pid"
    for _ in {1..50}; do
        [[ ! -e "/proc/$pid" ]] && return 0
        sleep 0.1
    done
    echo "Manual vision_service PID $pid did not stop." >&2
    exit 1
}

install_service() {
    require_root
    [[ -x "$VISION_BINARY" ]] || {
        echo "Missing executable: $VISION_BINARY" >&2
        exit 1
    }
    [[ -f "$UNIT_SOURCE" ]] || {
        echo "Missing unit file: $UNIT_SOURCE" >&2
        exit 1
    }

    install -d -m 0755 "$BACKUP_DIR"
    if [[ -e "$VIDEO_CONFIG" ]]; then
        [[ ! -e "$VIDEO_BACKUP" ]] || {
            echo "Both video config and backup exist; refusing to overwrite either." >&2
            exit 1
        }
        mv "$VIDEO_CONFIG" "$VIDEO_BACKUP"
    elif [[ ! -e "$VIDEO_BACKUP" ]]; then
        echo "Cannot find video_hub_pc4 config or its backup." >&2
        exit 1
    fi

    install -m 0644 "$UNIT_SOURCE" "$UNIT_TARGET"
    systemctl daemon-reload
    systemctl enable master_service.service
    systemctl restart master_service.service

    if ! systemctl is-active --quiet master_service.service; then
        echo "master_service failed to start." >&2
        exit 1
    fi
    if pgrep -x videohub_pc4 >/dev/null; then
        echo "video_hub_pc4 is still running; refusing to start vision_service." >&2
        exit 1
    fi

    systemctl stop vision_service.service 2>/dev/null || true
    stop_manual_vision_service
    systemctl enable --now vision_service.service
    for _ in {1..600}; do
        if [[ -S "$PROJECT_ROOT/run/vision_service.sock" ]] &&
           systemctl is-active --quiet vision_service.service; then
            echo "Installed: master_service is active, video_hub_pc4 is disabled, vision_service is ready."
            return 0
        fi
        if systemctl is-failed --quiet vision_service.service; then
            systemctl status vision_service.service --no-pager -l >&2 || true
            exit 1
        fi
        sleep 0.1
    done

    echo "vision_service did not become ready within 60 seconds." >&2
    exit 1
}

restore_service() {
    require_root
    systemctl disable --now vision_service.service 2>/dev/null || true
    rm -f "$UNIT_TARGET"

    if [[ -e "$VIDEO_BACKUP" ]]; then
        [[ ! -e "$VIDEO_CONFIG" ]] || {
            echo "Both video config and backup exist; refusing to overwrite either." >&2
            exit 1
        }
        mv "$VIDEO_BACKUP" "$VIDEO_CONFIG"
    fi

    systemctl daemon-reload
    systemctl restart master_service.service
    echo "Restored: vision_service autostart is removed and video_hub_pc4 is managed by master_service again."
}

show_status() {
    echo "master_service: $(systemctl is-active master_service.service 2>/dev/null || true)"
    echo "vision_service: $(systemctl is-active vision_service.service 2>/dev/null || true)"
    if pgrep -x vision_service >/dev/null; then
        echo "vision_service process: running"
    else
        echo "vision_service process: stopped"
    fi
    if [[ -e "$VIDEO_CONFIG" ]]; then
        echo "video_hub_pc4 config: enabled"
    elif [[ -e "$VIDEO_BACKUP" ]]; then
        echo "video_hub_pc4 config: disabled (backup present)"
    else
        echo "video_hub_pc4 config: missing (no backup)"
    fi
    if pgrep -x videohub_pc4 >/dev/null; then
        echo "video_hub_pc4 process: running"
    else
        echo "video_hub_pc4 process: stopped"
    fi
}

case "${1:-}" in
    install) install_service ;;
    restore) restore_service ;;
    status) show_status ;;
    *) usage; exit 2 ;;
esac
