# TAK Server Dash

A lightweight local web dashboard for Raspberry Pi OS 12/13 systems running TAK/OpenTAKServer-related services.

This is the GitHub-safe generic build. It contains no hardcoded private IP addresses, callsigns, SSIDs, ZeroTier peers, or custom infrastructure data.

It is designed to run alongside another dashboard if needed:

```text
Service: tak-server-dash.service
Port: 8091
App dir: /opt/tak-server-dash
Data dir: /var/lib/tak-server-dash
Env file: /etc/tak-server-dash.env
Wrapper: /usr/local/sbin/tak-server-dash-action
URL: http://<pi-ip-address>:8091/
```

## Install

Copy the ZIP to the Raspberry Pi, then run:

```bash
cd ~/Downloads
rm -rf tak-dashboard
unzip -o tak-server-dash-github-final-8091.zip
cd tak-dashboard
chmod +x install.sh uninstall.sh
sudo ./install.sh
sudo systemctl restart tak-server-dash.service
```

Open:

```text
http://<pi-ip-address>:8091/
```

## Verify

```bash
sudo systemctl status tak-server-dash.service --no-pager
sudo ss -ltnp | grep 8091
curl -s -o /tmp/tak-server-dash.html -w "%{http_code}\n" http://127.0.0.1:8091/
curl -s http://127.0.0.1:8091/api/status | head
```

A good local HTTP check returns:

```text
200
```

## Default monitored services

The default Services section monitors:

```text
opentakserver
eud_handler_ssl
rabbitmq-server
```

Users can add or remove services from the dashboard UI.

## Dashboard configuration UI

This build includes dashboard-side configuration controls:

- **ZeroTier Status**: add or remove peer IPs and callsigns from the section header.
- **Services**: add or remove systemd services from monitoring in the section header.
- **Banner Warnings**: choose which warning types appear in the top warning banner.

Runtime settings are stored locally in:

```text
/var/lib/tak-server-dash/config.json
```

Users do not need to edit the Python code.

## Environment configuration

Edit:

```bash
sudo nano /etc/tak-server-dash.env
```

Then restart:

```bash
sudo systemctl restart tak-server-dash.service
```

Supported settings:

```text
TAK_DASHBOARD_BIND=0.0.0.0
TAK_DASHBOARD_PORT=8091
TAK_DASHBOARD_DATA_DIR=/var/lib/tak-server-dash
TAK_DASHBOARD_AUTH=disabled

# Comma-separated systemd services shown by default.
TAK_DASHBOARD_SERVICES=opentakserver,eud_handler_ssl,rabbitmq-server

# Comma-separated services that trigger banner warnings if inactive.
TAK_DASHBOARD_REQUIRED_SERVICES=opentakserver,eud_handler_ssl,rabbitmq-server

# Use auto to detect interfaces, or specify comma-separated names.
TAK_DASHBOARD_INTERFACES=auto

# Optional ZeroTier peers. Users can also add these in the dashboard.
# CSV format:
# TAK_DASHBOARD_ZEROTIER_DEVICES=192.0.2.2:server,192.0.2.3:toc
#
# JSON format:
# TAK_DASHBOARD_ZEROTIER_DEVICES=[{"ip":"192.0.2.2","callsign":"server"}]
TAK_DASHBOARD_ZEROTIER_DEVICES=

# Optional HaLow/WiFi mesh SSID prefix. Leave blank to disable the HaLow latency row.
TAK_DASHBOARD_HALOW_SSID_PREFIX=

# Optional public ping target for Internet latency.
TAK_DASHBOARD_INTERNET_PING_TARGET=1.1.1.1
```

## Banner warnings

The top warning banner can be enabled/disabled by warning type from the dashboard.

Available warning checks:

```text
No network connected
Monitored service not active
CPU temp over 65 C
4G/LTE reception under 40%
Battery under 30%
Root disk warning at 40%
Root disk critical at 90%
Dashboard data stale over 60 seconds
Diagnostics folder over 300 MB
```

## WiFi / HaLow connection display

If `wlan0` has no active SSID and no IPv4 address, the WiFi/HaLow reception field displays:

```text
Not connected
```

## Optional packages

These packages improve data collection:

```bash
sudo apt update
sudo apt install -y network-manager wireless-tools iw i2c-tools libqmi-utils
```

## Authentication

Authentication is disabled by default because this dashboard is intended for trusted local, VPN, or field-network access only.

To enable basic auth:

```text
TAK_DASHBOARD_AUTH=enabled
TAK_DASHBOARD_USER=admin
TAK_DASHBOARD_PASSWORD=<your-password>
```

Then restart:

```bash
sudo systemctl restart tak-server-dash.service
```

## Useful commands

```bash
sudo systemctl status tak-server-dash.service --no-pager
sudo journalctl -u tak-server-dash.service -f
sudo systemctl restart tak-server-dash.service
sudo cat /etc/tak-server-dash.env
```

## Uninstall

```bash
cd tak-dashboard
sudo ./uninstall.sh
```

The uninstall script keeps `/etc/tak-server-dash.env` and `/var/lib/tak-server-dash` unless you remove them manually.

## Security note

This dashboard can start, stop, and restart configured services, run diagnostics, renew DHCP, and reboot/shutdown the Pi. Do not expose it directly to the public Internet.
