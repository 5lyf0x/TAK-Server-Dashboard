# Configuration

Main environment file:

```text
/etc/tak-server-dash.env
```

Runtime dashboard UI configuration:

```text
/var/lib/tak-server-dash/config.json
```

The dashboard UI can manage:

- ZeroTier peer IPs and callsigns
- Monitored services
- Top banner warning types

Restart after editing the env file manually:

```bash
sudo systemctl restart tak-server-dash.service
```

The dashboard UI settings do not require a service restart.
