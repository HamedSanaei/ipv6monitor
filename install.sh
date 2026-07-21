#!/usr/bin/env bash

set -Eeuo pipefail

PROJECT_NAME="ipv6monitor"
DEFAULT_RAW_BASE="https://raw.githubusercontent.com/HamedSanaei/ipv6monitor/main"
RAW_BASE="${IPV6MONITOR_RAW_BASE:-$DEFAULT_RAW_BASE}"
INSTALL_ROOT="/usr/local/lib/ipv6monitor"
BIN_PATH="/usr/local/bin/ipv6monitor"
CONFIG_DIR="/etc/ipv6monitor"
CONFIG_PATH="$CONFIG_DIR/ipv6monitor.conf"
UNIT_PATH="/etc/systemd/system/ipv6monitor.service"
UNINSTALL_PATH="/usr/local/sbin/ipv6monitor-uninstall"

log() {
    printf '[ipv6monitor] %s\n' "$*"
}

fail() {
    printf '[ipv6monitor] ERROR: %s\n' "$*" >&2
    exit 1
}

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
    fail "Run as root, for example: curl -fsSL $RAW_BASE/install.sh | sudo bash"
fi

if [[ ! -r /etc/os-release ]]; then
    fail "Cannot detect the operating system. Ubuntu or Debian is required."
fi

# shellcheck disable=SC1091
source /etc/os-release
case "${ID:-}" in
    ubuntu|debian)
        ;;
    *)
        if [[ " ${ID_LIKE:-} " != *" debian "* ]]; then
            fail "Unsupported distribution: ${PRETTY_NAME:-unknown}. Ubuntu/Debian is required."
        fi
        ;;
esac

command -v systemctl >/dev/null 2>&1 || fail "systemd/systemctl is required."
command -v apt-get >/dev/null 2>&1 || fail "apt-get is required."

export DEBIAN_FRONTEND=noninteractive
log "Installing required packages..."
apt-get update -y
apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    iproute2 \
    nftables \
    python3

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" 2>/dev/null && pwd -P || true)"
TEMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TEMP_DIR"' EXIT

install_project_file() {
    local relative_path="$1"
    local destination="$2"
    local mode="$3"
    local local_source="$SCRIPT_DIR/$relative_path"
    local staged_source="$TEMP_DIR/$(basename "$relative_path")"

    if [[ -n "$SCRIPT_DIR" && -f "$local_source" ]]; then
        install -D -m "$mode" "$local_source" "$destination"
    else
        log "Downloading $relative_path..."
        curl --proto '=https' --tlsv1.2 -fsSL \
            "$RAW_BASE/$relative_path" \
            -o "$staged_source"
        install -D -m "$mode" "$staged_source" "$destination"
    fi
}

if systemctl list-unit-files ipv6monitor.service --no-legend 2>/dev/null | grep -q '^ipv6monitor.service'; then
    log "Stopping the existing service for upgrade..."
    systemctl stop ipv6monitor.service || true
fi

log "Installing application files..."
install_project_file "src/ipv6monitor.py" "$INSTALL_ROOT/ipv6monitor.py" 0755
ln -sfn "$INSTALL_ROOT/ipv6monitor.py" "$BIN_PATH"
install_project_file "systemd/ipv6monitor.service" "$UNIT_PATH" 0644
install_project_file "uninstall.sh" "$UNINSTALL_PATH" 0755

mkdir -p "$CONFIG_DIR" /var/lib/ipv6monitor /run/ipv6monitor
chmod 0755 "$CONFIG_DIR" /var/lib/ipv6monitor /run/ipv6monitor

if [[ ! -f "$CONFIG_PATH" ]]; then
    log "Installing default configuration..."
    install_project_file "config/ipv6monitor.conf" "$CONFIG_PATH" 0644
else
    log "Keeping existing configuration: $CONFIG_PATH"
    if grep -Eq '^[[:space:]]*REFRESH_INTERVAL=0\.5[[:space:]]*$' "$CONFIG_PATH"; then
        log "Migrating the previous default refresh interval from 0.5s to 1s..."
        sed -i -E 's/^[[:space:]]*REFRESH_INTERVAL=0\.5[[:space:]]*$/REFRESH_INTERVAL=1/' "$CONFIG_PATH"
    fi
fi

log "Enabling the systemd service..."
systemctl daemon-reload
systemctl enable ipv6monitor.service >/dev/null
systemctl restart ipv6monitor.service

if ! systemctl is-active --quiet ipv6monitor.service; then
    systemctl --no-pager --full status ipv6monitor.service || true
    journalctl -u ipv6monitor.service -n 50 --no-pager || true
    fail "The service did not start successfully."
fi

log "Installation completed successfully."
echo
printf 'Run the live monitor with:\n\n  ipv6monitor\n\n'
printf 'Other useful commands:\n'
printf '  ipv6monitor status\n'
printf '  ipv6monitor history --hours 24\n'
printf '  ipv6monitor service-status\n'
printf '  sudo ipv6monitor reset\n'
printf '  sudo ipv6monitor-uninstall\n'
