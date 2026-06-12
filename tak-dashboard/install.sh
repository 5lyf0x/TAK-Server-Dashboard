#!/usr/bin/env bash
set -euo pipefail
APP_DIR="/opt/tak-server-dash"
DATA_DIR="/var/lib/tak-server-dash"
ENV_FILE="/etc/tak-server-dash.env"
SERVICE_FILE="/etc/systemd/system/tak-server-dash.service"
WRAPPER="/usr/local/sbin/tak-server-dash-action"
SUDOERS="/etc/sudoers.d/tak-server-dash"
USER_NAME="takserverdash"
if [[ "${EUID}" -ne 0 ]]; then echo "Run with sudo: sudo ./install.sh"; exit 1; fi
mkdir -p "$APP_DIR" "$DATA_DIR" "$DATA_DIR/diagnostics"
cp tak_dashboard.py "$APP_DIR/tak_dashboard.py"
chmod 0755 "$APP_DIR/tak_dashboard.py"
if ! id "$USER_NAME" >/dev/null 2>&1; then useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin "$USER_NAME"; fi
for grp in i2c gpio dialout netdev plugdev; do if getent group "$grp" >/dev/null 2>&1; then usermod -aG "$grp" "$USER_NAME" || true; fi; done
chown -R "$USER_NAME:$USER_NAME" "$DATA_DIR"
chmod 0750 "$DATA_DIR"
cat > "$WRAPPER" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
DEFAULT_ALLOWED_SERVICES="opentakserver eud_handler_ssl rabbitmq-server"
ENV_FILE="/etc/tak-server-dash.env"
if [[ -f "$ENV_FILE" ]]; then
  ALLOWED_SERVICES="$(grep -E '^TAK_DASHBOARD_SERVICES=' "$ENV_FILE" 2>/dev/null | tail -n1 | cut -d= -f2- | tr ',' ' ' || true)"
fi
ALLOWED_SERVICES="${ALLOWED_SERVICES:-$DEFAULT_ALLOWED_SERVICES}"
DATA_DIR="/var/lib/tak-server-dash"
DIAG_DIR="$DATA_DIR/diagnostics"
is_allowed_service() { local target="$1"; [[ "$target" =~ ^[A-Za-z0-9_.@-]{1,120}$ ]]; }
run_and_capture() { local title="$1"; shift; { echo; echo "===== $title ====="; echo "\$ $*"; "$@" 2>&1 || true; }; }
if [[ "${1:-}" == "service" ]]; then
  action="${2:-}"; service="${3:-}"
  case "$action" in start|stop|restart) ;; *) echo "Invalid action: $action" >&2; exit 2 ;; esac
  is_allowed_service "$service" || { echo "Service not allowed: $service" >&2; exit 2; }
  exec /usr/bin/systemctl "$action" "$service"
fi
if [[ "${1:-}" == "dhclient" ]]; then
  if command -v dhclient >/dev/null 2>&1; then exec "$(command -v dhclient)" -v wwan0; fi
  echo "dhclient not found" >&2; exit 127
fi
if [[ "${1:-}" == "diagnostics" ]]; then
  mkdir -p "$DIAG_DIR"; stamp="$(date -u +%Y%m%dT%H%M%SZ)"; tmpdir="$(mktemp -d)"; outfile="$DIAG_DIR/tak-dashboard-diagnostics-$stamp.tar.gz"; report="$tmpdir/report.txt"
  {
    echo "Mobile TAK Server Diagnostics"; echo "Generated UTC: $stamp"; echo "Hostname: $(hostname 2>/dev/null || true)"; echo
    run_and_capture "uname" uname -a
    run_and_capture "uptime" uptime
    run_and_capture "date" date -Is
    run_and_capture "ip addr" ip addr
    run_and_capture "ip route" ip route
    run_and_capture "ip route get 8.8.8.8" ip route get 8.8.8.8
    run_and_capture "resolv.conf" cat /etc/resolv.conf
    run_and_capture "NetworkManager devices" nmcli device status
    run_and_capture "NetworkManager active connections" nmcli connection show --active
    run_and_capture "ZeroTier status" zerotier-cli status
    run_and_capture "ZeroTier listnetworks" zerotier-cli listnetworks
    run_and_capture "df -h" df -h
    run_and_capture "free -h" free -h
    run_and_capture "vcgencmd get_throttled" vcgencmd get_throttled
    run_and_capture "CPU temp" sh -c 'cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null || true'
    run_and_capture "I2C detect bus 1" i2cdetect -y 1
    run_and_capture "Power supplies" sh -c 'for d in /sys/class/power_supply/*; do echo "--- $d"; grep -H . "$d"/* 2>/dev/null || true; done'
    run_and_capture "QMI devices" sh -c 'ls -l /dev/cdc-wdm* 2>/dev/null || true'
    run_and_capture "qmicli signal strength" sh -c 'for d in /dev/cdc-wdm*; do echo "--- $d"; qmicli -d "$d" --nas-get-signal-strength 2>&1 || true; done'
    run_and_capture "qmicli system info" sh -c 'for d in /dev/cdc-wdm*; do echo "--- $d"; qmicli -d "$d" --nas-get-system-info 2>&1 || true; done'
    for svc in $ALLOWED_SERVICES tak-dashboard zerotier-one NetworkManager; do
      run_and_capture "systemctl status $svc" systemctl status "$svc" --no-pager
      run_and_capture "journalctl $svc last 120 lines" journalctl -u "$svc" -n 120 --no-pager
    done
  } > "$report" 2>&1
  tar -czf "$outfile" -C "$tmpdir" report.txt
  chown takserverdash:takserverdash "$outfile" 2>/dev/null || true
  chmod 0640 "$outfile" 2>/dev/null || true
  rm -rf "$tmpdir"
  echo "$outfile"
  exit 0
fi
if [[ "${1:-}" == "system" ]]; then
  action="${2:-}"
  case "$action" in
    reboot)
      if command -v systemd-run >/dev/null 2>&1; then systemd-run --on-active=5 --unit=tak-dashboard-reboot /usr/bin/systemctl reboot; else /usr/sbin/shutdown -r +0 "TAK dashboard requested reboot"; fi
      echo "TAK server reboot scheduled."; exit 0 ;;
    shutdown)
      if command -v systemd-run >/dev/null 2>&1; then systemd-run --on-active=5 --unit=tak-dashboard-poweroff /usr/bin/systemctl poweroff; else /usr/sbin/shutdown -h +0 "TAK dashboard requested shutdown"; fi
      echo "TAK server shutdown scheduled."; exit 0 ;;
    *) echo "Invalid system action: $action" >&2; exit 2 ;;
  esac
fi
echo "Invalid command" >&2; exit 2
EOF
chmod 0755 "$WRAPPER"
chown root:root "$WRAPPER"
cat > "$SUDOERS" <<EOF
$USER_NAME ALL=(root) NOPASSWD: $WRAPPER *
EOF
chmod 0440 "$SUDOERS"
visudo -cf "$SUDOERS" >/dev/null
if [[ ! -f "$ENV_FILE" ]]; then
  PASSWORD="$(python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(18))
PY
)"
  cat > "$ENV_FILE" <<EOF
TAK_DASHBOARD_USER=admin
TAK_DASHBOARD_PASSWORD=$PASSWORD
TAK_DASHBOARD_BIND=0.0.0.0
TAK_DASHBOARD_PORT=8091
TAK_DASHBOARD_DATA_DIR=/var/lib/tak-server-dash
TAK_DASHBOARD_AUTH=disabled
TAK_DASHBOARD_SERVICES=opentakserver,eud_handler_ssl,rabbitmq-server
TAK_DASHBOARD_REQUIRED_SERVICES=opentakserver,eud_handler_ssl,rabbitmq-server
TAK_DASHBOARD_INTERFACES=auto
TAK_DASHBOARD_ZEROTIER_DEVICES=
TAK_DASHBOARD_HALOW_SSID_PREFIX=
TAK_DASHBOARD_INTERNET_PING_TARGET=1.1.1.1
EOF
  chmod 0600 "$ENV_FILE"
else
  PASSWORD="$(grep '^TAK_DASHBOARD_PASSWORD=' "$ENV_FILE" | cut -d= -f2- || true)"
fi
cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=TAK Server Dash
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER_NAME
Group=$USER_NAME
EnvironmentFile=$ENV_FILE
ExecStart=/usr/bin/python3 $APP_DIR/tak_dashboard.py
Restart=always
RestartSec=3
NoNewPrivileges=false

[Install]
WantedBy=multi-user.target
EOF

# Force known-good dashboard settings even when /etc/tak-server-dash.env already existed.
if grep -q '^TAK_DASHBOARD_PORT=' "$ENV_FILE"; then
  sed -i 's/^TAK_DASHBOARD_PORT=.*/TAK_DASHBOARD_PORT=8091/' "$ENV_FILE"
else
  echo 'TAK_DASHBOARD_PORT=8091' >> "$ENV_FILE"
fi

if grep -q '^TAK_DASHBOARD_AUTH=' "$ENV_FILE"; then
  sed -i 's/^TAK_DASHBOARD_AUTH=.*/TAK_DASHBOARD_AUTH=disabled/' "$ENV_FILE"
else
  echo 'TAK_DASHBOARD_AUTH=disabled' >> "$ENV_FILE"
fi


# Ensure generic configurable settings exist.
ensure_env_var() {
  local key="$1"
  local value="$2"
  if ! grep -q "^${key}=" "$ENV_FILE"; then
    echo "${key}=${value}" >> "$ENV_FILE"
  fi
}
ensure_env_var TAK_DASHBOARD_DATA_DIR "/var/lib/tak-server-dash"
ensure_env_var TAK_DASHBOARD_SERVICES "opentakserver,eud_handler_ssl,rabbitmq-server"
ensure_env_var TAK_DASHBOARD_REQUIRED_SERVICES "opentakserver,eud_handler_ssl,rabbitmq-server"
ensure_env_var TAK_DASHBOARD_INTERFACES "auto"
ensure_env_var TAK_DASHBOARD_ZEROTIER_DEVICES ""
ensure_env_var TAK_DASHBOARD_HALOW_SSID_PREFIX ""
ensure_env_var TAK_DASHBOARD_INTERNET_PING_TARGET "1.1.1.1"


# v46 default-service cleanup: remove older generic defaults from the env file.
if grep -q '^TAK_DASHBOARD_SERVICES=opentakserver,eud_handler_ssl,rabbitmq-server,4gmodem,adsbcot$' "$ENV_FILE"; then
  sed -i 's/^TAK_DASHBOARD_SERVICES=.*/TAK_DASHBOARD_SERVICES=opentakserver,eud_handler_ssl,rabbitmq-server/' "$ENV_FILE"
fi
if grep -q '^TAK_DASHBOARD_REQUIRED_SERVICES=opentakserver,eud_handler_ssl,rabbitmq-server,4gmodem,adsbcot$' "$ENV_FILE"; then
  sed -i 's/^TAK_DASHBOARD_REQUIRED_SERVICES=.*/TAK_DASHBOARD_REQUIRED_SERVICES=opentakserver,eud_handler_ssl,rabbitmq-server/' "$ENV_FILE"
fi

systemctl daemon-reload
systemctl enable --now tak-server-dash.service
echo
echo "Installed and started tak-server-dash.service"
echo
echo "Authentication: disabled"
echo "  Anyone who can reach the dashboard URL can use it."
echo
echo "Open from any reachable Pi IP:"
echo "  http://<PI_IP>:8091"
echo
echo "Detected IPv4 addresses:"
hostname -I | tr ' ' '\n' | sed '/^$/d' | while read -r ip; do echo "  http://$ip:8091"; done
echo
echo "Useful commands:"
echo "  sudo systemctl status tak-server-dash.service"
echo "  sudo journalctl -u tak-server-dash.service -f"
echo "  sudo cat /etc/tak-server-dash.env"
