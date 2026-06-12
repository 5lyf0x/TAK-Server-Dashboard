## Features

* **Raspberry Pi TAK/OpenTAKServer dashboard**

  * Lightweight local web dashboard for Raspberry Pi OS 12/13
  * Designed for TAK/OpenTAKServer field systems
  * Runs as a systemd service: `tak-server-dash.service`
  * Default port: `8091`
  * Listens on `0.0.0.0` so it can be reached over LAN, Wi-Fi, ZeroTier, Ethernet, or other reachable Pi interfaces

* **System health monitoring**

  * Hostname and timestamp
  * Uptime
  * Boot time
  * CPU load averages
  * RAM usage
  * Root disk usage
  * CPU temperature
  * CPU frequency
  * 24-hour CPU temperature history with high, average, and low values

* **Top warning banner**

  * Optional warning banner at the top of the dashboard
  * User-selectable warning types
  * Warns for:

    * No network connected
    * Monitored service not active
    * CPU temperature over 65 °C
    * 4G/LTE reception under 40%
    * Battery under 30%
    * Root disk usage warning at 40%
    * Root disk usage critical at 90%
    * Dashboard data stale for more than 60 seconds
    * Diagnostics folder over 300 MB

* **Network status**

  * Internet connectivity check
  * DNS lookup check
  * Active/default route interface detection
  * Live download rate
  * Live upload rate
  * Interface counter-based bandwidth display

* **Interface monitoring**

  * Auto-detects common Pi network interfaces
  * Shows interface state, MAC address, and IPv4 addresses
  * Supports interfaces such as:

    * Ethernet
    * Wi-Fi
    * 4G/LTE `wwan0`
    * ZeroTier `zt*` interfaces

* **Wi-Fi / HaLow connection status**

  * Shows Wi-Fi SSID
  * Shows `wlan0` IP address
  * Shows subnet mask
  * Shows gateway
  * Shows Wi-Fi reception percentage and signal bars
  * Displays `Not connected` when `wlan0` is disconnected
  * Optional HaLow/Wi-Fi mesh SSID prefix monitoring

* **4G/LTE status**

  * Checks `wwan0` state
  * Checks IPv4 presence
  * Checks default route status
  * Attempts QMI status through `qmicli` / `qmi-network` when available
  * Attempts AT serial fallback when available
  * Shows signal percentage
  * Shows signal dBm when available
  * Shows operator when available
  * Shows access technology when available
  * 4G/LTE reception bars use color thresholds:

    * 0–40% red
    * 41–70% orange
    * 71–100% green

* **Power / runtime status**

  * Reads Linux `/sys/class/power_supply` data when available
  * Supports MAX1704x-style I2C fuel gauge detection
  * Shows battery percentage when available
  * Shows battery voltage when available

* **Network latency checks**

  * Internet latency check
  * Wi-Fi gateway latency check
  * Optional HaLow/Wi-Fi mesh gateway latency check
  * Optional ZeroTier peer latency check
  * ZeroTier latency reports the fastest responding configured peer

* **ZeroTier peer monitoring**

  * Add ZeroTier peer IPs from the dashboard UI
  * Add callsigns/labels for peers
  * Remove peers from the dashboard UI
  * Shows peer status
  * Shows latency when available
  * Shows last active time

* **Configurable service monitoring**

  * Default monitored services:

    * `opentakserver`
    * `eud_handler_ssl`
    * `rabbitmq-server`
  * Add additional systemd services from the dashboard UI
  * Remove services from the dashboard UI
  * Start, stop, and restart monitored services from the dashboard
  * Shows service load state, active state, substate, and enabled state

* **USB device status**

  * Detects QMI modem devices
  * Detects likely 4G/LTE USB modem hardware
  * Detects RTL-SDR / ADS-B receiver hints
  * Detects USB serial devices
  * Detects USB storage devices
  * Shows USB bus/device count

* **Diagnostics tools**

  * Generate a diagnostics bundle from the dashboard
  * Download generated diagnostics bundle
  * View diagnostics folder size

* Clear old diagnostics bundles from the dashboard

- **TAK server power controls**

  * Reboot the Raspberry Pi from the dashboard
  * Shut down the Raspberry Pi from the dashboard
  * Confirmation prompts help prevent accidental power actions

- **Privacy mode**

  * Privacy Mode starts enabled on page load
  * Sensitive network values are blurred
  * Toggle between Privacy Mode and Normal Mode
  * Floating mode indicator stays visible while scrolling

- **Dashboard section controls**

  * Show or hide dashboard sections
  * Section visibility is saved in the browser
  * Compact dropdown UI keeps the dashboard clean

- **Mobile-friendly layout**

  * Responsive layout for phones, tablets, and small screens
  * Tables convert to stacked card-style rows on small screens
  * Designed to avoid horizontal side-scrolling on mobile browsers

- **Local runtime configuration**

  * Runtime UI settings are stored locally in:

    ```text
    /var/lib/tak-server-dash/config.json
    ```
  * Environment configuration is stored in:

    ```text
    /etc/tak-server-dash.env
    ```

- **No-login default for trusted networks**

  * Authentication is disabled by default for local/trusted deployments
  * Basic authentication can be enabled through the environment file
  * Intended for trusted LAN, VPN, ZeroTier, HaLow/Wi-Fi, or wired admin networks
