# Quickstart

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

Verify:

```bash
curl -s -o /tmp/tak-server-dash.html -w "%{http_code}\n" http://127.0.0.1:8091/
```
