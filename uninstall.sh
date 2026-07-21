#!/usr/bin/env bash

set -Eeuo pipefail

PURGE_DATA=false
for argument in "$@"; do
    case "$argument" in
        --purge)
            PURGE_DATA=true
            ;;
        -h|--help)
            cat <<'HELP'
Usage: sudo ipv6monitor-uninstall [--purge]

Without --purge, the persistent SQLite database and configuration are kept.
With --purge, all ipv6monitor configuration and traffic history are deleted.
HELP
            exit 0
            ;;
        *)
            printf 'Unknown option: %s\n' "$argument" >&2
            exit 2
            ;;
    esac
done

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
    echo "Run as root: sudo ipv6monitor-uninstall" >&2
    exit 1
fi

if command -v systemctl >/dev/null 2>&1; then
    systemctl disable --now ipv6monitor.service >/dev/null 2>&1 || true
fi

if command -v nft >/dev/null 2>&1; then
    nft delete table inet ipv6monitor >/dev/null 2>&1 || true
fi

rm -f /etc/systemd/system/ipv6monitor.service
rm -f /usr/local/bin/ipv6monitor
rm -rf /usr/local/lib/ipv6monitor
rm -f /usr/local/sbin/ipv6monitor-uninstall
rm -rf /run/ipv6monitor

if [[ "$PURGE_DATA" == true ]]; then
    rm -rf /etc/ipv6monitor /var/lib/ipv6monitor
    echo "ipv6monitor and all persistent data were removed."
else
    echo "ipv6monitor was removed. Configuration and persistent data were kept."
    echo "Use --purge during uninstall to delete them too."
fi

if command -v systemctl >/dev/null 2>&1; then
    systemctl daemon-reload
    systemctl reset-failed ipv6monitor.service >/dev/null 2>&1 || true
fi
