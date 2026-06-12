#!/usr/bin/env bash
set -euo pipefail
if [[ "${EUID}" -ne 0 ]]; then echo "Run with sudo: sudo ./uninstall.sh"; exit 1; fi
systemctl disable --now tak-server-dash.service 2>/dev/null || true
rm -f /etc/systemd/system/tak-server-dash.service
systemctl daemon-reload
rm -f /usr/local/sbin/tak-server-dash-action
rm -f /etc/sudoers.d/tak-server-dash
rm -rf /opt/tak-server-dash
echo "Removed Pi TAK Dashboard service and app files."
echo "Kept /etc/tak-server-dash.env and /var/lib/tak-server-dash in case you want your password/history/diagnostics."
echo "To remove those too:"
echo "  sudo rm -f /etc/tak-server-dash.env"
echo "  sudo rm -rf /var/lib/tak-server-dash"
