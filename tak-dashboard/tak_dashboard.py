#!/usr/bin/env python3
import base64, json, os, re, shutil, subprocess, threading, time, select, termios
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

BIND=os.environ.get('TAK_DASHBOARD_BIND','0.0.0.0')
PORT=int(os.environ.get('TAK_DASHBOARD_PORT','8091'))
AUTH_USER=os.environ.get('TAK_DASHBOARD_USER','admin')
AUTH_PASS=os.environ.get('TAK_DASHBOARD_PASSWORD','change-me-now')
DATA_DIR=Path(os.environ.get('TAK_DASHBOARD_DATA_DIR','/var/lib/tak-server-dash'))
TEMP_HISTORY_FILE=DATA_DIR/'temp_history.json'
ZT_LAST_ACTIVE_FILE=DATA_DIR/'zerotier_last_active.json'
NET_RATE_FILE=DATA_DIR/'net_rate.json'
DIAG_DIR=DATA_DIR/'diagnostics'
CONFIG_FILE=DATA_DIR/'config.json'

def env_list(name, default=''):
    raw=os.environ.get(name, default)
    return [x.strip() for x in re.split(r'[, ]+', raw or '') if x.strip()]

def env_zerotier_devices():
    raw=(os.environ.get('TAK_DASHBOARD_ZEROTIER_DEVICES','') or '').strip()
    if not raw:
        return []
    # Supported:
    #   JSON: [{"ip":"10.0.0.2","callsign":"node-a"}]
    #   CSV:  10.0.0.2:node-a,10.0.0.3:node-b
    try:
        data=json.loads(raw)
        out=[]
        if isinstance(data,list):
            for item in data:
                if isinstance(item,dict) and item.get('ip'):
                    out.append({'ip':str(item.get('ip','')).strip(),'callsign':str(item.get('callsign') or item.get('name') or item.get('ip')).strip()})
        return out
    except Exception:
        pass
    out=[]
    for part in raw.split(','):
        part=part.strip()
        if not part:
            continue
        if ':' in part:
            ip,label=part.split(':',1)
        else:
            ip,label=part,part
        out.append({'ip':ip.strip(),'callsign':label.strip()})
    return out

# GitHub-safe defaults. Override in /etc/tak-server-dash.env.
SERVICES = env_list('TAK_DASHBOARD_SERVICES','opentakserver,eud_handler_ssl,rabbitmq-server')
REQUIRED_SERVICES = env_list('TAK_DASHBOARD_REQUIRED_SERVICES','opentakserver,eud_handler_ssl,rabbitmq-server')
INTERFACES = env_list('TAK_DASHBOARD_INTERFACES','auto')
ZEROTIER_DEVICES = env_zerotier_devices()
HALOW_SSID_PREFIX = os.environ.get('TAK_DASHBOARD_HALOW_SSID_PREFIX','').strip()


CONFIG_VERSION = 2
LEGACY_DEFAULT_SERVICES_TO_REMOVE = {'4gmodem','adsbcot'}

DEFAULT_WARNING_SETTINGS = {
    "network": True,
    "services": True,
    "cpu_temp": True,
    "lte_reception": True,
    "battery": True,
    "disk": True,
    "stale": True,
    "diagnostics": True,
}

WARNING_LABELS = {
    "network": "No network connected",
    "services": "Monitored service not active",
    "cpu_temp": "CPU temp over 65 C",
    "lte_reception": "4G/LTE reception under 40%",
    "battery": "Battery under 30%",
    "disk": "Root disk warning/critical",
    "stale": "Dashboard data stale over 60s",
    "diagnostics": "Diagnostics folder over 300 MB",
}

def sanitize_service_name(name):
    name = str(name or "").strip()
    if not re.match(r"^[A-Za-z0-9_.@-]{1,120}$", name):
        return ""
    return name

def sanitize_host_value(host):
    host = str(host or "").strip()
    if not re.match(r"^[A-Za-z0-9_.:-]{1,160}$", host):
        return ""
    return host

def normalize_zt_devices(items):
    out = []
    seen = set()
    if not isinstance(items, list):
        return out
    for item in items:
        if not isinstance(item, dict):
            continue
        ip = sanitize_host_value(item.get("ip", ""))
        if not ip or ip in seen:
            continue
        callsign = str(item.get("callsign") or item.get("name") or ip).strip()[:80]
        out.append({"ip": ip, "callsign": callsign})
        seen.add(ip)
    return out

def normalize_services(items):
    out = []
    seen = set()
    if not isinstance(items, list):
        return out
    for item in items:
        svc = sanitize_service_name(item)
        if svc and svc not in seen:
            out.append(svc)
            seen.add(svc)
    return out

def default_runtime_config():
    return {
        "config_version": CONFIG_VERSION,
        "zerotier_devices": normalize_zt_devices(ZEROTIER_DEVICES),
        "services": normalize_services(SERVICES),
        "required_services": normalize_services(REQUIRED_SERVICES),
        "warning_settings": dict(DEFAULT_WARNING_SETTINGS),
    }

def load_runtime_config():
    cfg = default_runtime_config()
    raw = load_json_file(CONFIG_FILE, {})
    if isinstance(raw, dict):
        raw_version = int(raw.get("config_version") or 1)
        if "zerotier_devices" in raw:
            cfg["zerotier_devices"] = normalize_zt_devices(raw.get("zerotier_devices"))
        if "services" in raw:
            services = normalize_services(raw.get("services"))
            if raw_version < 2:
                services = [s for s in services if s not in LEGACY_DEFAULT_SERVICES_TO_REMOVE]
            if services:
                cfg["services"] = services
        if "required_services" in raw:
            req = normalize_services(raw.get("required_services"))
            if raw_version < 2:
                req = [s for s in req if s not in LEGACY_DEFAULT_SERVICES_TO_REMOVE]
            cfg["required_services"] = req
        warnings = raw.get("warning_settings")
        if isinstance(warnings, dict):
            merged = dict(DEFAULT_WARNING_SETTINGS)
            for k in merged:
                if k in warnings:
                    merged[k] = bool(warnings[k])
            cfg["warning_settings"] = merged
        cfg["config_version"] = CONFIG_VERSION
    return cfg

def save_runtime_config(cfg):
    clean = default_runtime_config()
    clean["config_version"] = CONFIG_VERSION
    if isinstance(cfg, dict):
        clean["zerotier_devices"] = normalize_zt_devices(cfg.get("zerotier_devices", clean["zerotier_devices"]))
        services = normalize_services(cfg.get("services", clean["services"]))
        clean["services"] = services or normalize_services(SERVICES)
        req = normalize_services(cfg.get("required_services", clean["required_services"]))
        clean["required_services"] = req
        warnings = cfg.get("warning_settings", clean["warning_settings"])
        merged = dict(DEFAULT_WARNING_SETTINGS)
        if isinstance(warnings, dict):
            for k in merged:
                if k in warnings:
                    merged[k] = bool(warnings[k])
        clean["warning_settings"] = merged
    atomic_write_json(CONFIG_FILE, clean)
    return clean

def effective_zerotier_devices():
    return load_runtime_config().get("zerotier_devices", [])

def effective_services():
    return load_runtime_config().get("services", normalize_services(SERVICES))

def effective_required_services():
    cfg = load_runtime_config()
    req = cfg.get("required_services", [])
    return req if req else cfg.get("services", normalize_services(SERVICES))

def warning_settings():
    return load_runtime_config().get("warning_settings", dict(DEFAULT_WARNING_SETTINGS))


def run_cmd(cmd,timeout=5):
    try:
        p=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=timeout,check=False)
        return {'ok':p.returncode==0,'rc':p.returncode,'stdout':p.stdout.strip(),'stderr':p.stderr.strip(),'cmd':cmd}
    except FileNotFoundError:
        return {'ok':False,'rc':127,'stdout':'','stderr':f'Command not found: {cmd[0]}','cmd':cmd}
    except subprocess.TimeoutExpired:
        return {'ok':False,'rc':124,'stdout':'','stderr':f'Command timed out after {timeout}s','cmd':cmd}
    except Exception as e:
        return {'ok':False,'rc':1,'stdout':'','stderr':str(e),'cmd':cmd}

def label_interface(dev):
    if dev=='wwan0': return '4G/LTE'
    if dev=='wlan0': return 'WiFi'
    if dev.startswith('zt'): return 'ZeroTier'
    return dev

def prefix_to_netmask(prefix):
    try:
        import ipaddress
        return str(ipaddress.IPv4Network(f'0.0.0.0/{int(prefix)}').netmask)
    except Exception: return ''

def get_ipv4_details(dev):
    r=run_cmd(['ip','-j','addr','show','dev',dev],3); out=[]
    if not r['ok']: return out
    try:
        for item in json.loads(r['stdout']):
            for a in item.get('addr_info',[]):
                if a.get('family')=='inet':
                    p=a.get('prefixlen'); ip=a.get('local','')
                    out.append({'address':ip,'prefixlen':p,'cidr':f'{ip}/{p}','netmask':prefix_to_netmask(p)})
    except Exception: pass
    return out

def get_ipv4s(dev): return [d['cidr'] for d in get_ipv4_details(dev)]
def get_zt_interfaces(): return sorted(p.name for p in Path('/sys/class/net').glob('zt*'))

def get_interface_status(dev):
    ipv4s = get_ipv4s(dev)
    mac = ""
    raw_operstate = "unknown"
    flags = []

    link = run_cmd(["ip", "-j", "link", "show", "dev", dev], timeout=3)
    if link["ok"]:
        try:
            data = json.loads(link["stdout"])
            if data:
                item = data[0]
                raw_operstate = str(item.get("operstate", "unknown")).lower()
                flags = item.get("flags", []) or []
                mac = item.get("address", "") or ""
        except Exception:
            pass

    if not mac:
        try:
            mac = Path(f"/sys/class/net/{dev}/address").read_text().strip()
        except Exception:
            pass

    # Field-friendly interface state:
    # Many wwan/tun interfaces report UNKNOWN even when they are usable.
    # If the interface has an IPv4 address, show it as connected.
    if ipv4s:
        status = "connected"
    else:
        flag_set = set(str(f).upper() for f in flags)
        if "LOWER_UP" in flag_set:
            status = "link-up"
        elif "UP" in flag_set:
            status = "up"
        elif raw_operstate and raw_operstate != "unknown":
            status = raw_operstate
        else:
            status = "down"

    return {
        "name": dev,
        "label": label_interface(dev),
        "ipv4": ipv4s,
        "operstate": status,
        "raw_operstate": raw_operstate,
        "flags": flags,
        "mac": mac,
    }


def get_interfaces():
    if INTERFACES == ['auto'] or not INTERFACES:
        skip_prefixes=('lo','docker','br-','veth','virbr')
        names=[]
        try:
            for p in sorted(Path('/sys/class/net').iterdir()):
                n=p.name
                if n == 'lo' or any(n.startswith(x) for x in skip_prefixes):
                    continue
                names.append(n)
        except Exception:
            names=['eth0','wlan0','wwan0']
    else:
        names=list(INTERFACES)
    for z in get_zt_interfaces():
        if z not in names:
            names.append(z)
    return {n:get_interface_status(n) for n in names}

def get_gateway_for_dev(dev):
    r=run_cmd(['bash','-lc',f"nmcli -g IP4.GATEWAY device show {dev} 2>/dev/null | head -n1"],4)
    if r['ok'] and r['stdout'].strip(): return r['stdout'].strip()
    r=run_cmd(['bash','-lc',f"ip route | awk '/default .* dev {dev}/{{print $3; exit}}'"],3)
    if r['ok'] and r['stdout'].strip(): return r['stdout'].strip()
    det=get_ipv4_details(dev)
    if det:
        try:
            import ipaddress
            hosts=list(ipaddress.IPv4Interface(det[0]['cidr']).network.hosts())
            if hosts: return f'{hosts[0]} (inferred)'
        except Exception: pass
    return ''

def bars_from_percent(v):
    try: p=int(float(v))
    except Exception: return {'count':None,'text':'unknown'}
    c=0 if p<=0 else 1 if p<=20 else 2 if p<=40 else 3 if p<=60 else 4 if p<=80 else 5
    return {'count':c,'text':'▮'*c+'▯'*(5-c)}

def wifi_signal_percent():
    # 1) NetworkManager WiFi scan/list: connected row is marked with "*".
    cmds = [
        ['bash','-lc',"nmcli -t -f IN-USE,SIGNAL dev wifi list ifname wlan0 2>/dev/null | awk -F: '$1==\"*\"{print $2; exit}'"],
        ['bash','-lc',"nmcli -t -f IN-USE,SIGNAL dev wifi 2>/dev/null | awk -F: '$1==\"*\"{print $2; exit}'"],
        ['bash','-lc',"nmcli -t -f ACTIVE,SIGNAL dev wifi 2>/dev/null | awk -F: '$1==\"yes\"{print $2; exit}'"],
    ]
    for cmd in cmds:
        r=run_cmd(cmd,4)
        if r['ok'] and r['stdout'].strip():
            try:
                return str(max(0,min(100,int(float(r['stdout'].strip())))))
            except Exception:
                pass

    # 2) iw link: signal is usually dBm. Convert roughly: -90 dBm = 0%, -40 dBm = 100%.
    r=run_cmd(['bash','-lc',"iw dev wlan0 link 2>/dev/null | awk '/signal:/ {print $2; exit}'"],4)
    if r['ok'] and r['stdout'].strip():
        try:
            dbm=float(r['stdout'].strip())
            return str(max(0,min(100,int((dbm + 90) * 2))))
        except Exception:
            pass

    # 3) /proc/net/wireless: link quality usually out of 70.
    try:
        for line in Path('/proc/net/wireless').read_text().splitlines():
            if line.strip().startswith('wlan0:'):
                parts=line.replace(':',' ').split()
                if len(parts) >= 3:
                    quality=float(parts[2].strip('.'))
                    if quality <= 70:
                        return str(max(0,min(100,int((quality / 70.0) * 100))))
                    return str(max(0,min(100,int(quality))))
    except Exception:
        pass

    # 4) iwconfig fallback if wireless-tools is installed.
    r=run_cmd(['bash','-lc',"iwconfig wlan0 2>/dev/null"],4)
    if r['ok'] and r['stdout']:
        m=re.search(r'Link Quality=([0-9.]+)/([0-9.]+)', r['stdout'])
        if m:
            try:
                return str(max(0,min(100,int((float(m.group(1))/float(m.group(2))) * 100))))
            except Exception:
                pass
        m=re.search(r'Signal level=(-?[0-9.]+) dBm', r['stdout'])
        if m:
            try:
                dbm=float(m.group(1))
                return str(max(0,min(100,int((dbm + 90) * 2))))
            except Exception:
                pass
    return ''

def get_wifi_info():
    info={'ssid':'','device':'wlan0','ip_address':'','subnet_mask':'','gateway':'','signal_percent':None,'bars':{'count':None,'text':'Not connected'},'connected':False}
    r=run_cmd(['iwgetid','wlan0','-r'],4)
    if r['ok'] and r['stdout'].strip(): info['ssid']=r['stdout'].strip()
    else:
        r=run_cmd(['bash','-lc',"nmcli -t -f ACTIVE,SSID dev wifi | awk -F: '$1==\"yes\"{print $2; exit}'"],4)
        if r['ok'] and r['stdout'].strip(): info['ssid']=r['stdout'].strip()
    d=get_ipv4_details('wlan0')
    if d:
        info['ip_address']=d[0].get('address',''); info['subnet_mask']=d[0].get('netmask','')
    info['gateway']=get_gateway_for_dev('wlan0')

    # If wlan0 has no active SSID and no IPv4 address, show a clean disconnected state.
    info['connected']=bool(info.get('ssid') or info.get('ip_address'))
    if info['connected']:
        sig=wifi_signal_percent()
        try:
            if sig not in ('', None):
                info['signal_percent']=int(float(sig))
                info['bars']=bars_from_percent(info['signal_percent'])
            else:
                info['signal_percent']=None
                info['bars']={'count':None,'text':'unknown'}
        except Exception:
            info['signal_percent']=None
            info['bars']={'count':None,'text':'unknown'}
    else:
        info['signal_percent']=None
        info['bars']={'count':None,'text':'Not connected'}
    return info



def format_bits_per_sec(bps):
    try:
        bps = float(bps)
    except Exception:
        return "unknown"
    units = ["bps", "Kbps", "Mbps", "Gbps"]
    val = bps
    unit = units[0]
    for unit in units:
        if val < 1000 or unit == units[-1]:
            break
        val /= 1000.0
    if unit == "bps":
        return f"{int(round(val))} {unit}"
    return f"{val:.2f} {unit}"

def get_default_route_device():
    for target in ("1.1.1.1", "8.8.8.8"):
        r = run_cmd(["ip", "route", "get", target], timeout=3)
        if r["ok"]:
            m = re.search(r"\bdev\s+(\S+)", r["stdout"])
            if m:
                return m.group(1)
    r = run_cmd(["ip", "route", "show", "default"], timeout=3)
    if r["ok"]:
        for line in r["stdout"].splitlines():
            m = re.search(r"\bdev\s+(\S+)", line)
            if m:
                return m.group(1)
    return ""

def read_interface_counters(dev):
    if not dev:
        return None
    base = Path("/sys/class/net") / dev / "statistics"
    try:
        rx = int((base / "rx_bytes").read_text().strip())
        tx = int((base / "tx_bytes").read_text().strip())
        return {"rx_bytes": rx, "tx_bytes": tx}
    except Exception:
        return None

def get_bandwidth_status():
    dev = get_default_route_device()
    counters = read_interface_counters(dev)
    now = time.time()
    if not dev or counters is None:
        return {
            "interface": dev or "unknown",
            "download_bps": None,
            "upload_bps": None,
            "download": "unknown",
            "upload": "unknown",
            "note": "Interface counters unavailable.",
        }

    last = load_json_file(NET_RATE_FILE, {})
    if not isinstance(last, dict):
        last = {}
    last_for_dev = last.get(dev, {}) if isinstance(last.get(dev, {}), dict) else {}
    dt = now - float(last_for_dev.get("ts", 0) or 0)
    rx_prev = last_for_dev.get("rx_bytes")
    tx_prev = last_for_dev.get("tx_bytes")

    last[dev] = {
        "ts": now,
        "rx_bytes": counters["rx_bytes"],
        "tx_bytes": counters["tx_bytes"],
    }
    atomic_write_json(NET_RATE_FILE, last)

    if rx_prev is None or tx_prev is None or dt < 1:
        return {
            "interface": dev,
            "download_bps": None,
            "upload_bps": None,
            "download": "measuring",
            "upload": "measuring",
            "note": "Passive rate from interface byte counters. Updates after the next refresh.",
        }

    rx_delta = counters["rx_bytes"] - int(rx_prev)
    tx_delta = counters["tx_bytes"] - int(tx_prev)
    if rx_delta < 0 or tx_delta < 0:
        return {
            "interface": dev,
            "download_bps": None,
            "upload_bps": None,
            "download": "measuring",
            "upload": "measuring",
            "note": "Counter reset detected. Measuring again.",
        }

    down_bps = (rx_delta * 8.0) / dt
    up_bps = (tx_delta * 8.0) / dt

    return {
        "interface": dev,
        "download_bps": round(down_bps),
        "upload_bps": round(up_bps),
        "download": format_bits_per_sec(down_bps),
        "upload": format_bits_per_sec(up_bps),
        "note": "Live passive throughput on the default internet interface. This is current usage, not a maximum speed test.",
    }


def get_internet_status():
    p=run_cmd(['ping','-c','1','-W','2','8.8.8.8'],4)
    d=run_cmd(['getent','hosts','google.com'],5)
    state='UP' if p['ok'] and d['ok'] else 'IP ONLY / DNS FAIL' if p['ok'] else 'DOWN'
    return {'state':state,'ping_ip':p['ok'],'dns_lookup':d['ok'],'dns_result':d['stdout'] if d['ok'] else d['stderr'],'bandwidth':get_bandwidth_status()}


def read_text_file(path):
    try:
        return Path(path).read_text().strip()
    except Exception:
        return ""

def percent_from_dbm(dbm, weak=-115, strong=-75):
    try:
        dbm = float(dbm)
        pct = int(round(((dbm - weak) / (strong - weak)) * 100))
        return max(0, min(100, pct))
    except Exception:
        return None

def first_qmi_device():
    try:
        devices = sorted(str(p) for p in Path("/dev").glob("cdc-wdm*"))
        return devices[0] if devices else ""
    except Exception:
        return ""

def parse_qmi_signal(text):
    result = {"percent": None, "dbm": None, "tech": ""}
    if not text:
        return result

    # Prefer LTE RSRP when available. Fall back to RSSI/current signal.
    patterns = [
        ("rsrp", r"RSRP:.*?(-?\d+(?:\.\d+)?)\s*dBm", -120, -80),
        ("rssi", r"RSSI:.*?(-?\d+(?:\.\d+)?)\s*dBm", -110, -50),
        ("current", r"Current:.*?(-?\d+(?:\.\d+)?)\s*dBm", -110, -50),
    ]
    for _name, pattern, weak, strong in patterns:
        m = re.search(pattern, text, re.I | re.S)
        if m:
            result["dbm"] = float(m.group(1))
            result["percent"] = percent_from_dbm(result["dbm"], weak, strong)
            break

    mtech = re.search(r"Network\s+'([^']+)'", text, re.I)
    if mtech:
        result["tech"] = mtech.group(1).upper()
    return result

def parse_qmi_tech_and_operator(*texts):
    tech = ""
    operator = ""
    combined = "\n".join(t for t in texts if t)

    if re.search(r"LTE\s+service:.*?Status:\s*'available'", combined, re.I | re.S):
        tech = "LTE"
    elif re.search(r"Radio interfaces:\s*'([^']+)'", combined, re.I):
        radios = re.findall(r"Radio interfaces:\s*'([^']+)'", combined, re.I)
        tech = ", ".join(r.upper() for r in radios)
    elif "LTE" in combined.upper():
        tech = "LTE"

    for pattern in [
        r"Operator name:\s*'([^']+)'",
        r"Description:\s*'([^']+)'",
        r"Network description:\s*'([^']+)'",
    ]:
        m = re.search(pattern, combined, re.I)
        if m:
            operator = m.group(1)
            break

    return tech, operator

def get_qmi_status(qmi_dev):
    q = {
        "device": qmi_dev or "",
        "qmi_available": False,
        "qmi_network_status": "",
        "signal_percent": None,
        "signal_dbm": None,
        "tech": "",
        "operator": "",
        "note": "",
    }
    if not qmi_dev:
        q["note"] = "No /dev/cdc-wdm* QMI device found."
        return q

    if not shutil.which("qmicli"):
        q["note"] = "qmicli not installed; using wwan0/sysfs only."
        return q

    q["qmi_available"] = True

    sig = run_cmd(["qmicli", "-d", qmi_dev, "--nas-get-signal-strength"], 7)
    sysinfo = run_cmd(["qmicli", "-d", qmi_dev, "--nas-get-system-info"], 7)
    serving = run_cmd(["qmicli", "-d", qmi_dev, "--nas-get-serving-system"], 7)
    home = run_cmd(["qmicli", "-d", qmi_dev, "--nas-get-home-network"], 7)

    if sig["ok"]:
        parsed = parse_qmi_signal(sig["stdout"])
        q["signal_percent"] = parsed.get("percent")
        q["signal_dbm"] = parsed.get("dbm")
        q["tech"] = parsed.get("tech") or ""

    tech, operator = parse_qmi_tech_and_operator(
        sysinfo["stdout"] if sysinfo["ok"] else "",
        serving["stdout"] if serving["ok"] else "",
        home["stdout"] if home["ok"] else "",
    )
    if tech:
        q["tech"] = tech
    q["operator"] = operator

    if shutil.which("qmi-network"):
        qnet = run_cmd(["qmi-network", qmi_dev, "status"], 5)
        if qnet["ok"]:
            q["qmi_network_status"] = " ".join(qnet["stdout"].split())
        else:
            q["qmi_network_status"] = qnet["stderr"] or ""

    notes = []
    if not sig["ok"]:
        notes.append("qmicli signal query failed")
    if not sysinfo["ok"] and not serving["ok"]:
        notes.append("qmicli registration query failed")
    q["note"] = "; ".join(notes)
    return q


LTE_AT_CACHE = {"ts": 0, "port": "", "data": {}}

TECH_MAP = {
    "0": "GSM",
    "1": "GSM COMPACT",
    "2": "UMTS",
    "3": "EDGE",
    "4": "HSDPA",
    "5": "HSUPA",
    "6": "HSPA",
    "7": "LTE",
    "8": "EC-GSM-IOT",
    "9": "LTE CAT-M1",
    "10": "NB-IOT",
}

def serial_at_command(port, command, timeout=1.4):
    fd = None
    old = None
    try:
        fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        old = termios.tcgetattr(fd)
        attrs = termios.tcgetattr(fd)
        attrs[0] = attrs[0] & ~(termios.IGNBRK | termios.BRKINT | termios.PARMRK | termios.ISTRIP | termios.INLCR | termios.IGNCR | termios.ICRNL | termios.IXON)
        attrs[1] = attrs[1] & ~termios.OPOST
        attrs[2] = attrs[2] | termios.CLOCAL | termios.CREAD
        attrs[2] = attrs[2] & ~(termios.PARENB | termios.PARODD | termios.CSTOPB | termios.CSIZE)
        attrs[2] = attrs[2] | termios.CS8
        attrs[3] = attrs[3] & ~(termios.ECHO | termios.ECHONL | termios.ICANON | termios.ISIG | termios.IEXTEN)
        attrs[4] = termios.B115200
        attrs[5] = termios.B115200
        attrs[6][termios.VMIN] = 0
        attrs[6][termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        termios.tcflush(fd, termios.TCIOFLUSH)
        os.write(fd, (command.strip() + "\r").encode("ascii", "ignore"))
        chunks = []
        end = time.time() + timeout
        while time.time() < end:
            r, _, _ = select.select([fd], [], [], 0.12)
            if fd in r:
                try:
                    data = os.read(fd, 4096)
                except BlockingIOError:
                    data = b""
                if data:
                    chunks.append(data)
                    text = b"".join(chunks).decode("utf-8", "ignore")
                    upper = text.upper()
                    if "\nOK" in upper or "\rOK" in upper or "\nERROR" in upper or "\rERROR" in upper:
                        break
        return b"".join(chunks).decode("utf-8", "ignore")
    except Exception as e:
        return f"ERROR: {e}"
    finally:
        try:
            if fd is not None and old is not None:
                termios.tcsetattr(fd, termios.TCSANOW, old)
        except Exception:
            pass
        try:
            if fd is not None:
                os.close(fd)
        except Exception:
            pass

def list_serial_candidates():
    try:
        ports = sorted(str(p) for p in Path('/dev').glob('ttyUSB*')) + sorted(str(p) for p in Path('/dev').glob('ttyACM*'))
        return ports[:12]
    except Exception:
        return []

def find_at_port():
    cached = LTE_AT_CACHE.get('port') or ''
    candidates = ([cached] if cached else []) + [p for p in list_serial_candidates() if p != cached]
    for port in candidates:
        if not port:
            continue
        out = serial_at_command(port, 'AT', 0.8)
        if re.search(r'(^|\r|\n)OK(\r|\n|$)', out, re.I):
            LTE_AT_CACHE['port'] = port
            return port
    return ''

def parse_csq(text):
    m = re.search(r'\+CSQ:\s*(\d+)\s*,\s*(\d+)', text, re.I)
    if not m:
        return {}
    rssi = int(m.group(1))
    out = {'csq': rssi}
    if 0 <= rssi <= 31:
        dbm = -113 + (2 * rssi)
        out['signal_dbm'] = dbm
        out['signal_percent'] = max(0, min(100, round((rssi / 31) * 100)))
    return out

def parse_cesq(text):
    m = re.search(r'\+CESQ:\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)', text, re.I)
    if not m:
        return {}
    vals = [int(x) for x in m.groups()]
    rxlev, ber, rscp, ecno, rsrq, rsrp = vals
    out = {'cesq': vals}
    if 0 <= rsrp <= 97:
        # 3GPP TS 27.007: RSRP = -140 dBm + value, 255 unknown
        dbm = -140 + rsrp
        out['rsrp_dbm'] = dbm
        out['signal_dbm'] = dbm
        out['signal_percent'] = max(0, min(100, round((dbm + 120) * 2.5)))
    if 0 <= rsrq <= 34:
        out['rsrq_db'] = round(-19.5 + 0.5 * rsrq, 1)
    return out

def parse_cops(text):
    m = re.search(r'\+COPS:\s*(\d+)\s*,\s*(\d+)\s*,\s*"?([^",\r\n]*)"?\s*(?:,\s*(\d+))?', text, re.I)
    if not m:
        return {}
    operator = (m.group(3) or '').strip()
    tech_code = (m.group(4) or '').strip()
    return {'operator': operator, 'tech': TECH_MAP.get(tech_code, tech_code)}

def parse_registration(text):
    statuses = []
    for name in ('CREG', 'CGREG', 'CEREG'):
        m = re.search(r'\+' + name + r':\s*(?:\d+\s*,\s*)?(\d+)', text, re.I)
        if m:
            code = m.group(1)
            label = {'0':'not registered','1':'registered home','2':'searching','3':'denied','4':'unknown','5':'registered roaming'}.get(code, code)
            statuses.append(f"{name}={label}")
    return ', '.join(statuses)

def get_at_lte_status():
    now = time.time()
    if LTE_AT_CACHE.get('data') and now - LTE_AT_CACHE.get('ts', 0) < 30:
        return LTE_AT_CACHE['data']
    out = {'available': False, 'port': '', 'operator': '', 'tech': '', 'signal_percent': None, 'signal_dbm': None, 'bars': None, 'note': ''}
    ports = list_serial_candidates()
    if not ports:
        out['note'] = 'No /dev/ttyUSB* or /dev/ttyACM* AT ports found.'
        LTE_AT_CACHE.update({'ts': now, 'data': out})
        return out
    port = find_at_port()
    if not port:
        out['note'] = 'No responsive AT command port found; check dialout permissions or modem serial mode.'
        LTE_AT_CACHE.update({'ts': now, 'data': out})
        return out
    out['available'] = True
    out['port'] = port
    csq_txt = serial_at_command(port, 'AT+CSQ', 1.2)
    cesq_txt = serial_at_command(port, 'AT+CESQ', 1.2)
    cops_txt = serial_at_command(port, 'AT+COPS?', 1.6)
    reg_txt = '\n'.join([
        serial_at_command(port, 'AT+CREG?', 1.0),
        serial_at_command(port, 'AT+CGREG?', 1.0),
        serial_at_command(port, 'AT+CEREG?', 1.0),
    ])
    csq = parse_csq(csq_txt)
    cesq = parse_cesq(cesq_txt)
    cops = parse_cops(cops_txt)
    # Prefer LTE RSRP from CESQ if available; fall back to CSQ RSSI.
    sig = cesq if cesq.get('signal_percent') is not None else csq
    if sig.get('signal_percent') is not None:
        out['signal_percent'] = sig['signal_percent']
        out['bars'] = bars_from_percent(sig['signal_percent'])
    if sig.get('signal_dbm') is not None:
        out['signal_dbm'] = sig['signal_dbm']
    if cops.get('operator'):
        out['operator'] = cops['operator']
    if cops.get('tech'):
        out['tech'] = cops['tech']
    notes = [f"AT serial={port}"]
    if csq.get('signal_dbm') is not None:
        notes.append(f"CSQ={csq.get('csq')} ({csq.get('signal_dbm')} dBm)")
    if cesq.get('rsrp_dbm') is not None:
        notes.append(f"RSRP={cesq.get('rsrp_dbm')} dBm")
    if cesq.get('rsrq_db') is not None:
        notes.append(f"RSRQ={cesq.get('rsrq_db')} dB")
    reg = parse_registration(reg_txt)
    if reg:
        notes.append(reg)
    out['note'] = ' · '.join(notes)
    LTE_AT_CACHE.update({'ts': now, 'data': out})
    return out


def get_lte_status():
    l = {
        "available": False,
        "state": "unknown",
        "access_technology": "",
        "operator": "",
        "signal_quality_percent": None,
        "signal_dbm": None,
        "bars": {"count": None, "text": "unknown"},
        "modem": "",
        "note": "",
    }

    wwan_ips = get_ipv4s("wwan0")
    iface = get_interface_status("wwan0")
    default_dev = get_default_route_device()
    carrier = read_text_file("/sys/class/net/wwan0/carrier")
    operstate = read_text_file("/sys/class/net/wwan0/operstate") or iface.get("raw_operstate", "unknown")
    svc = run_cmd(["systemctl", "is-active", "4gmodem"], 3)
    service_state = svc["stdout"].strip() if svc["ok"] else "unknown"

    qmi_dev = first_qmi_device()
    qmi = get_qmi_status(qmi_dev)
    at = get_at_lte_status()

    l["modem"] = qmi_dev or at.get("port") or "wwan0"
    l["available"] = bool(wwan_ips or qmi_dev or at.get("available") or carrier == "1")

    if qmi.get("signal_percent") is not None:
        l["signal_quality_percent"] = qmi["signal_percent"]
        l["signal_dbm"] = qmi.get("signal_dbm")
        l["bars"] = bars_from_percent(qmi["signal_percent"])
    elif at.get("signal_percent") is not None:
        l["signal_quality_percent"] = at["signal_percent"]
        l["signal_dbm"] = at.get("signal_dbm")
        l["bars"] = at.get("bars") or bars_from_percent(at["signal_percent"])

    if qmi.get("tech"):
        l["access_technology"] = str(qmi["tech"]).upper()
    elif at.get("tech"):
        l["access_technology"] = str(at["tech"]).upper()

    if qmi.get("operator"):
        l["operator"] = qmi["operator"]
    elif at.get("operator"):
        l["operator"] = at["operator"]

    qnet = (qmi.get("qmi_network_status") or "").lower()

    if wwan_ips and default_dev == "wwan0":
        l["state"] = "connected"
    elif wwan_ips:
        l["state"] = "wwan0 has IPv4"
    elif "connected" in qnet or "started" in qnet:
        l["state"] = "modem connected"
    elif service_state == "active":
        l["state"] = "4gmodem active"
    elif carrier == "1":
        l["state"] = "link detected"
    elif qmi_dev or at.get("available"):
        l["state"] = "modem detected"
    elif iface.get("operstate") and iface.get("operstate") != "down":
        l["state"] = iface.get("operstate")
    else:
        l["state"] = "not detected"

    note_parts = [
        "Source: wwan0/sysfs + qmicli/qmi-network + AT serial fallback; ModemManager not used.",
        f"wwan0={operstate}",
        f"4gmodem={service_state}",
    ]
    if default_dev:
        note_parts.append(f"default route={default_dev}")
    if wwan_ips:
        note_parts.append("IPv4 present")
    if qmi.get("signal_dbm") is not None:
        note_parts.append(f"qmi signal={qmi['signal_dbm']} dBm")
    if qmi.get("qmi_network_status"):
        note_parts.append(f"qmi-network={qmi['qmi_network_status']}")
    if qmi.get("note"):
        note_parts.append(qmi["note"])
    if at.get("note"):
        note_parts.append(at["note"])

    l["note"] = ""
    return l

def get_service_status(svc):
    r=run_cmd(['systemctl','show',svc,'--no-pager','--property=LoadState,ActiveState,SubState,UnitFileState'],5)
    s={'name':svc,'load':'unknown','active':'unknown','sub':'unknown','enabled':'unknown'}
    if r['ok']:
        for line in r['stdout'].splitlines():
            if '=' in line:
                k,v=line.split('=',1)
                if k=='LoadState': s['load']=v
                elif k=='ActiveState': s['active']=v
                elif k=='SubState': s['sub']=v
                elif k=='UnitFileState': s['enabled']=v
    else: s.update({'load':'not-found','sub':r['stderr']})
    return s

def get_services(): return {s:get_service_status(s) for s in effective_services()}

def read_cpu_temp_c():
    for p in ['/sys/class/thermal/thermal_zone0/temp','/sys/class/hwmon/hwmon0/temp1_input']:
        try:
            v=float(Path(p).read_text().strip()); return round(v/1000 if v>1000 else v,1)
        except Exception: pass
    return None

def load_json_file(p,default):
    try: return json.loads(p.read_text())
    except Exception: return default

def atomic_write_json(p,data):
    DATA_DIR.mkdir(parents=True,exist_ok=True); tmp=p.with_suffix('.tmp'); tmp.write_text(json.dumps(data)); tmp.replace(p)

def temp_sampler_loop():
    while True:
        try:
            t=read_cpu_temp_c(); now=time.time()
            if t is not None:
                h=load_json_file(TEMP_HISTORY_FILE,[]); h.append({'ts':now,'temp_c':t}); h=[x for x in h if x.get('ts',0)>=now-86400]; atomic_write_json(TEMP_HISTORY_FILE,h)
        except Exception: pass
        time.sleep(60)

def read_cpu_freq_mhz():
    paths = [
        "/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq",
        "/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_cur_freq",
    ]
    for path in paths:
        try:
            raw = Path(path).read_text().strip()
            khz = float(raw)
            return round(khz / 1000.0)
        except Exception:
            pass
    return None


def get_cpu_info():
    current = read_cpu_temp_c()
    hist = load_json_file(TEMP_HISTORY_FILE, [])
    now = time.time()
    cutoff = now - 86400
    recent = [x for x in hist if x.get("ts", 0) >= cutoff and x.get("temp_c") is not None]
    temps = [float(x["temp_c"]) for x in recent]

    high = max(temps, default=current)
    low = min(temps, default=current)
    avg = round(sum(temps) / len(temps), 1) if temps else current

    return {
        "current_c": current,
        "highest_24h_c": high,
        "lowest_24h_c": low,
        "average_24h_c": avg,
        "samples_24h": len(recent),
        "frequency_mhz": read_cpu_freq_mhz(),
        "note": "24h stats begin collecting after dashboard installation/start.",
    }


def ping_zt_device(d):
    r=run_cmd(['ping','-n','-c','1','-W','1',d['ip']],3); lat=''
    if r['ok']:
        m=re.search(r'time=([0-9.]+)\s*ms',r['stdout']); lat=m.group(1) if m else ''
    return {'ip':d['ip'],'callsign':d['callsign'],'online':r['ok'],'status':'ONLINE' if r['ok'] else 'OFFLINE','latency_ms':lat}

def get_zerotier_connectivity():
    devices = effective_zerotier_devices()
    if not devices:
        return []
    last=load_json_file(ZT_LAST_ACTIVE_FILE,{}) if isinstance(load_json_file(ZT_LAST_ACTIVE_FILE,{}),dict) else {}; now=int(time.time()); byip={}
    with ThreadPoolExecutor(max_workers=min(8,len(devices))) as ex:
        fmap={ex.submit(ping_zt_device,d):d for d in devices}
        for fut in as_completed(fmap):
            d=fmap[fut]
            try: item=fut.result()
            except Exception as e: item={'ip':d['ip'],'callsign':d.get('callsign',d['ip']),'online':False,'status':'ERROR','latency_ms':'','error':str(e)}
            if item['online']: last[item['ip']]=now
            item['last_active_ts']=last.get(item['ip']); byip[item['ip']]=item
    atomic_write_json(ZT_LAST_ACTIVE_FILE,last)
    return [byip.get(d['ip'],{'ip':d['ip'],'callsign':d.get('callsign',d['ip']),'status':'UNKNOWN','latency_ms':'','last_active_ts':last.get(d['ip'])}) for d in devices]

def format_duration(sec):
    try: sec=int(sec)
    except Exception: return 'unknown'
    d,rem=divmod(sec,86400); h,rem=divmod(rem,3600); m,_=divmod(rem,60)
    return f'{d}d {h}h {m}m' if d else f'{h}h {m}m' if h else f'{m}m'

def parse_throttled(raw):
    try: val=int(raw.strip().split('=')[-1],16)
    except Exception: return {'raw':raw.strip() if raw else '','value':None,'status':'unknown','flags':['unable to parse']}
    meanings=[(0,'currently under-voltage'),(1,'currently frequency capped'),(2,'currently throttled'),(3,'currently soft temperature limit active'),(16,'under-voltage has occurred'),(17,'frequency capping has occurred'),(18,'throttling has occurred'),(19,'soft temperature limit has occurred')]
    flags=[label for bit,label in meanings if val & (1<<bit)]
    return {'raw':raw.strip(),'value':val,'status':'OK' if val==0 else 'CHECK','flags':flags if flags else ['none']}

def get_system_health():
    h={}
    try:
        up=float(Path('/proc/uptime').read_text().split()[0]); h['uptime']=format_duration(up); h['boot_time']=datetime.fromtimestamp(time.time()-up).strftime('%Y-%m-%d %H:%M:%S')
    except Exception: h['uptime']='unknown'; h['boot_time']='unknown'
    try: la=os.getloadavg(); h['load_average']=[round(float(la[0]),1), round(float(la[1]),1), round(float(la[2]),1)]
    except Exception: h['load_average']=[]
    mem={}
    try:
        for line in Path('/proc/meminfo').read_text().splitlines():
            if ':' in line:
                k,v=line.split(':',1); parts=v.strip().split()
                if parts and parts[0].isdigit(): mem[k]=int(parts[0])
    except Exception: pass
    if mem.get('MemTotal') and mem.get('MemAvailable') is not None:
        used=mem['MemTotal']-mem['MemAvailable']; pct=used/mem['MemTotal']*100; h['ram']={'text':f'{round(used/1024)} / {round(mem["MemTotal"]/1024)} MB ({pct:.1f}%)'}
    else: h['ram']={'text':'unknown'}
    try:
        du=shutil.disk_usage('/'); pct=du.used/du.total*100; h['disk']={'text':f'{du.used/(1024**3):.1f} / {du.total/(1024**3):.1f} GB ({pct:.1f}%)','used_percent':round(pct,1),'used_gb':round(du.used/(1024**3),1),'total_gb':round(du.total/(1024**3),1)}
    except Exception: h['disk']={'text':'unknown','used_percent':None}
    vc=run_cmd(['vcgencmd','get_throttled'],3); h['throttling']=parse_throttled(vc['stdout']) if vc['ok'] else {'raw':'','value':None,'status':'unknown','flags':['vcgencmd unavailable']}
    return h

def read_sysfs_power():
    base=Path('/sys/class/power_supply'); out=[]
    if not base.exists(): return out
    for s in sorted(base.iterdir()):
        item={'name':s.name}
        for k in ['type','status','capacity','voltage_now','current_now','power_now','energy_now','charge_now']:
            try: item[k]=(s/k).read_text().strip()
            except Exception: pass
        try:
            if 'voltage_now' in item: item['voltage_v']=round(float(item['voltage_now'])/1_000_000,2)
        except Exception: pass
        try:
            if 'current_now' in item: item['current_a']=round(float(item['current_now'])/1_000_000,2)
        except Exception: pass
        out.append(item)
    return out

def i2c_byte(bus,addr,reg):
    r=run_cmd(['i2cget','-y',str(bus),hex(addr),hex(reg),'b'],3)
    if not r['ok']: raise RuntimeError(r['stderr'] or r['stdout'] or 'i2cget failed')
    return int(r['stdout'].strip(),16)

def read_fuel_gauge():
    bus=int(os.environ.get('TAK_DASHBOARD_I2C_BUS','1')); addr=int(os.environ.get('TAK_DASHBOARD_FUEL_GAUGE_ADDR','0x36'),16)
    v=((i2c_byte(bus,addr,0x02)<<8)|i2c_byte(bus,addr,0x03))>>4
    soc=i2c_byte(bus,addr,0x04)+(i2c_byte(bus,addr,0x05)/256.0)
    return {'source':f'i2c bus {bus}, address 0x{addr:02x}','voltage_v':round(v*0.00125,3),'percent':round(soc,1)}


def read_hwmon_power_sensors():
    sensors = []
    try:
        for h in sorted(Path('/sys/class/hwmon').glob('hwmon*')):
            item = {'path': str(h), 'name': read_text_file(str(h / 'name')) or h.name}
            currents = []
            powers = []
            volts = []
            for p in h.glob('curr*_input'):
                try:
                    currents.append(float(p.read_text().strip()) / 1000.0)  # hwmon current is mA
                except Exception:
                    pass
            for p in h.glob('power*_input'):
                try:
                    powers.append(float(p.read_text().strip()) / 1000000.0)  # hwmon power is uW
                except Exception:
                    pass
            for p in h.glob('in*_input'):
                try:
                    volts.append(float(p.read_text().strip()) / 1000.0)  # hwmon voltage is mV
                except Exception:
                    pass
            if currents:
                item['current_a'] = round(max(currents), 3)
            if powers:
                item['power_w'] = round(max(powers), 3)
            # Only use voltage if it looks like a realistic supply/battery/input voltage.
            good_volts = [v for v in volts if 3.0 <= v <= 30.0]
            if good_volts:
                item['voltage_v'] = round(max(good_volts), 3)
            if 'current_a' in item or 'power_w' in item:
                sensors.append(item)
    except Exception:
        pass
    return sensors

def read_vcgencmd_power():
    out = {'current_a': None, 'voltage_v': None, 'power_w': None, 'source': '', 'note': ''}
    if not shutil.which('vcgencmd'):
        return out
    r = run_cmd(['vcgencmd', 'pmic_read_adc'], 5)
    if not r.get('ok') or not r.get('stdout'):
        return out
    currents = []
    volts = []
    for line in r['stdout'].splitlines():
        label = line.split()[0] if line.split() else ''
        m_cur = re.search(r'current\(\d+\)=([0-9.]+)A', line)
        m_volt = re.search(r'volt\(\d+\)=([0-9.]+)V', line)
        # Prefer input/vbus/ext5v style rails. Ignore tiny/internal rails.
        label_ok = re.search(r'(EXT5V|VBUS|USB|VIN|VSYS|BATT)', label, re.I)
        if m_cur and label_ok:
            try:
                currents.append((label, float(m_cur.group(1))))
            except Exception:
                pass
        if m_volt and label_ok:
            try:
                v = float(m_volt.group(1))
                if 3.0 <= v <= 30.0:
                    volts.append((label, v))
            except Exception:
                pass
    if currents:
        label, val = max(currents, key=lambda x: x[1])
        out['current_a'] = round(val, 3)
        out['source'] = f'vcgencmd pmic_read_adc ({label})'
    if volts:
        label, val = max(volts, key=lambda x: x[1])
        out['voltage_v'] = round(val, 3)
        if not out['source']:
            out['source'] = f'vcgencmd pmic_read_adc ({label})'
    if out['current_a'] is not None and out['voltage_v'] is not None:
        out['power_w'] = round(out['current_a'] * out['voltage_v'], 2)
    return out

def estimate_runtime_hours(percent=None, power_w=None):
    try:
        battery_wh = float(os.environ.get('TAK_DASHBOARD_BATTERY_WH', '0') or 0)
    except Exception:
        battery_wh = 0
    if battery_wh > 0 and percent is not None and power_w and power_w > 0:
        return (battery_wh * (float(percent) / 100.0)) / float(power_w)
    return None

def format_runtime_hours(hours):
    if hours is None:
        return ''
    try:
        h = float(hours)
        if h < 0:
            return ''
        if h < 1:
            return f"{round(h*60)} min"
        return f"{int(h)}h {round((h-int(h))*60)}m"
    except Exception:
        return ''


def get_power_runtime():
    p={'available':False,'status':'unknown','source':'unknown','battery_percent':None,'battery_voltage_v':None,'current_a':None,'power_w':None,'runtime_estimate':'','details':'','supplies':[],'current_source':''}
    supplies=read_sysfs_power(); p['supplies']=supplies
    for s in supplies:
        if s.get('type','').lower() in ('battery','ups') or 'bat' in s.get('name','').lower():
            p.update({'available':True,'source':f"/sys/class/power_supply/{s.get('name')}",'status':s.get('status','unknown'),'details':'Read from Linux power_supply interface.'})
            if s.get('capacity'):
                try: p['battery_percent']=round(float(s['capacity']),1)
                except Exception: pass
            p['battery_voltage_v']=s.get('voltage_v')
            p['current_a']=s.get('current_a')
            if p['current_a'] is not None:
                p['current_source']=p['source']
            try:
                if s.get('power_now'):
                    p['power_w']=round(float(s['power_now'])/1_000_000,2)
                elif p['current_a'] is not None and p['battery_voltage_v'] is not None:
                    p['power_w']=round(float(p['current_a'])*float(p['battery_voltage_v']),2)
            except Exception:
                pass
            # Runtime from sysfs energy/power if available.
            try:
                if s.get('energy_now') and s.get('power_now') and float(s['power_now']) > 0:
                    hours=float(s['energy_now'])/float(s['power_now'])
                    p['runtime_estimate']=format_runtime_hours(hours)
            except Exception:
                pass
            if not p['runtime_estimate']:
                p['runtime_estimate']=format_runtime_hours(estimate_runtime_hours(p['battery_percent'], p.get('power_w')))
            return p
    try:
        fg=read_fuel_gauge()
        p.update({'available':True,'source':fg['source'],'status':'fuel gauge detected','battery_percent':fg['percent'],'battery_voltage_v':fg['voltage_v']})
        details=['Stats in this card are for internal 18650 batteries, not any connected powerbank.']
    except Exception as e:
        details=[f'No standard power_supply battery found and I2C fuel gauge read failed: {e}']

    # Try generic current/power sensors even when the fuel gauge only exposes voltage/SOC.
    hw = read_hwmon_power_sensors()
    if hw:
        chosen = hw[0]
        p['current_a'] = chosen.get('current_a')
        p['power_w'] = chosen.get('power_w')
        p['current_source'] = f"hwmon {chosen.get('name')}"
        if p.get('battery_voltage_v') is None and chosen.get('voltage_v') is not None:
            p['battery_voltage_v'] = chosen.get('voltage_v')
        details.append(f"Current/power from {p['current_source']}.")
    else:
        vcg = read_vcgencmd_power()
        if vcg.get('current_a') is not None:
            p['current_a'] = vcg.get('current_a')
            p['current_source'] = vcg.get('source') or 'vcgencmd pmic_read_adc'
            details.append(f"Current from {p['current_source']}.")
        if vcg.get('power_w') is not None:
            p['power_w'] = vcg.get('power_w')
        if p.get('battery_voltage_v') is None and vcg.get('voltage_v') is not None:
            p['battery_voltage_v'] = vcg.get('voltage_v')

    if p.get('power_w') is None and p.get('current_a') is not None and p.get('battery_voltage_v') is not None:
        try:
            p['power_w'] = round(float(p['current_a']) * float(p['battery_voltage_v']), 2)
        except Exception:
            pass

    p['runtime_estimate'] = format_runtime_hours(estimate_runtime_hours(p.get('battery_percent'), p.get('power_w')))
    if p['runtime_estimate']:
        details.append('Runtime estimate uses TAK_DASHBOARD_BATTERY_WH and measured/estimated power draw.')
    elif p.get('current_a') is None:
        pass
    else:
        details.append('Current found, but runtime needs TAK_DASHBOARD_BATTERY_WH to estimate remaining time.')
    p['details']=' '.join(details)
    return p



def clean_host_for_ping(value):
    if not value:
        return ""
    m = re.search(r"(\d{1,3}(?:\.\d{1,3}){3})", str(value))
    return m.group(1) if m else str(value).strip()

def get_default_gateway():
    r = run_cmd(["ip", "route", "show", "default"], 3)
    if not r["ok"]:
        return {"gateway": "", "dev": ""}
    for line in r["stdout"].splitlines():
        m_gateway = re.search(r"\bvia\s+(\S+)", line)
        m_dev = re.search(r"\bdev\s+(\S+)", line)
        if m_gateway or m_dev:
            return {
                "gateway": m_gateway.group(1) if m_gateway else "",
                "dev": m_dev.group(1) if m_dev else "",
            }
    return {"gateway": "", "dev": ""}

def ping_latency_target(item):
    host = clean_host_for_ping(item.get("host", ""))
    if not host:
        return {
            "name": item.get("name", "unknown"),
            "host": item.get("host_label", ""),
            "status": item.get("empty_status", "UNKNOWN"),
            "latency_ms": "",
            "note": item.get("note", "No host available"),
        }
    r = run_cmd(["ping", "-n", "-c", "1", "-W", "1", host], 3)
    latency = ""
    if r["ok"]:
        m = re.search(r"time=([0-9.]+)\s*ms", r["stdout"])
        latency = m.group(1) if m else ""
    return {
        "name": item.get("name", host),
        "host": item.get("host_label", host),
        "status": "ONLINE" if r["ok"] else "OFFLINE",
        "latency_ms": latency,
        "note": item.get("note", ""),
    }

def get_local_zerotier_ips():
    local = set()
    try:
        for p in Path("/sys/class/net").iterdir():
            if p.name.startswith("zt"):
                for addr in get_ipv4s(p.name):
                    ip = str(addr).split("/")[0].strip()
                    if ip:
                        local.add(ip)
    except Exception:
        pass
    return local

def get_zerotier_fastest_latency():
    devices = effective_zerotier_devices()
    local_ips = get_local_zerotier_ips()

    candidates = []
    for d in devices:
        ip = d.get("ip", "")
        if not ip or ip in local_ips:
            continue
        candidates.append(d)

    if not candidates:
        return {
            "name": "ZeroTier",
            "host": "",
            "status": "No clients available",
            "latency_ms": "",
            "note": "No ZeroTier peers configured.",
        }

    results = []
    with ThreadPoolExecutor(max_workers=min(8, len(candidates))) as ex:
        fmap = {ex.submit(ping_latency_target, {
            "name": "ZeroTier",
            "host": d.get("ip", ""),
            "host_label": d.get("ip", ""),
            "note": d.get("callsign", ""),
        }): d for d in candidates}
        for fut in as_completed(fmap):
            d = fmap[fut]
            try:
                item = fut.result()
            except Exception:
                item = {
                    "name": "ZeroTier",
                    "host": d.get("ip", ""),
                    "status": "ERROR",
                    "latency_ms": "",
                    "note": d.get("callsign", ""),
                }
            item["callsign"] = d.get("callsign", "")
            results.append(item)

    online = []
    for item in results:
        try:
            if item.get("status") == "ONLINE" and item.get("latency_ms") not in ("", None):
                online.append((float(item["latency_ms"]), item))
        except Exception:
            pass

    if not online:
        return {
            "name": "ZeroTier",
            "host": "",
            "status": "No clients available",
            "latency_ms": "",
            "note": "No configured ZeroTier peer responded.",
        }

    best_latency, best = sorted(online, key=lambda x: x[0])[0]
    note = f"Fastest client: {best.get('callsign') or best.get('host')}"
    return {
        "name": "ZeroTier",
        "host": best.get("host", ""),
        "status": "ONLINE",
        "latency_ms": best.get("latency_ms", ""),
        "note": note,
    }

    candidates = []
    for d in ZEROTIER_DEVICES:
        ip = d.get("ip", "")
        if not ip or ip in local_ips:
            continue
        candidates.append(d)

    if not candidates:
        return {
            "name": "ZeroTier",
            "host": "",
            "status": "No clients available",
            "latency_ms": "",
            "note": "No other ZeroTier devices are configured or local device could not be separated.",
        }

    results = []
    with ThreadPoolExecutor(max_workers=min(8, len(candidates))) as ex:
        fmap = {ex.submit(ping_latency_target, {
            "name": "ZeroTier",
            "host": d.get("ip", ""),
            "host_label": d.get("ip", ""),
            "note": d.get("callsign", ""),
        }): d for d in candidates}
        for fut in as_completed(fmap):
            d = fmap[fut]
            try:
                item = fut.result()
            except Exception:
                item = {
                    "name": "ZeroTier",
                    "host": d.get("ip", ""),
                    "status": "ERROR",
                    "latency_ms": "",
                    "note": d.get("callsign", ""),
                }
            item["callsign"] = d.get("callsign", "")
            results.append(item)

    online = []
    for item in results:
        try:
            if item.get("status") == "ONLINE" and item.get("latency_ms") not in ("", None):
                online.append((float(item["latency_ms"]), item))
        except Exception:
            pass

    if not online:
        return {
            "name": "ZeroTier",
            "host": "",
            "status": "No clients available",
            "latency_ms": "",
            "note": "No other ZeroTier client responded.",
        }

    best_latency, best = sorted(online, key=lambda x: x[0])[0]
    note = f"Fastest client: {best.get('callsign') or best.get('host')}"
    return {
        "name": "ZeroTier",
        "host": best.get("host", ""),
        "status": "ONLINE",
        "latency_ms": best.get("latency_ms", ""),
        "note": note,
    }

def get_network_latency():
    rows = []

    rows.append(ping_latency_target({
        "name": "Internet",
        "host": os.environ.get("TAK_DASHBOARD_INTERNET_PING_TARGET", "1.1.1.1"),
        "note": "Public ping target.",
    }))

    wifi_gateway = clean_host_for_ping(get_gateway_for_dev("wlan0"))
    if wifi_gateway:
        rows.append(ping_latency_target({
            "name": "WiFi",
            "host": wifi_gateway,
            "note": "Gateway ping of current WiFi network.",
        }))
    else:
        rows.append({
            "name": "WiFi",
            "host": "",
            "status": "DISCONNECTED",
            "latency_ms": "",
            "note": "No wlan0 gateway detected.",
        })

    if HALOW_SSID_PREFIX:
        wifi = get_wifi_info()
        ssid = wifi.get("ssid", "") or ""
        if ssid.startswith(HALOW_SSID_PREFIX):
            halow_gateway = clean_host_for_ping(wifi.get("gateway") or get_gateway_for_dev("wlan0"))
            if halow_gateway:
                rows.append(ping_latency_target({
                    "name": "HaLow",
                    "host": halow_gateway,
                    "note": "Gateway ping if connected to configured HaLow/WiFi mesh SSID.",
                }))
            else:
                rows.append({
                    "name": "HaLow",
                    "host": "",
                    "status": "DISCONNECTED",
                    "latency_ms": "",
                    "note": "Configured HaLow/WiFi mesh SSID detected, but no gateway was found.",
                })
        else:
            rows.append({
                "name": "HaLow",
                "host": "",
                "status": "DISCONNECTED",
                "latency_ms": "",
                "note": "Set TAK_DASHBOARD_HALOW_SSID_PREFIX to monitor a HaLow/WiFi mesh SSID.",
            })

    rows.append(get_zerotier_fastest_latency())
    return rows


def usb_glob(pattern):
    try:
        return sorted(str(p) for p in Path("/dev").glob(pattern))
    except Exception:
        return []

def get_usb_storage_devices():
    r = run_cmd(["lsblk", "-ndo", "NAME,TRAN,TYPE,SIZE,MODEL"], 4)
    if not r["ok"]:
        return []
    devices = []
    for line in r["stdout"].splitlines():
        parts = line.split(None, 4)
        if len(parts) >= 3 and parts[1] == "usb":
            devices.append(line.strip())
    return devices

def summarize_lsusb(lines, max_items=5):
    if not lines:
        return "none"
    shown = lines[:max_items]
    extra = len(lines) - len(shown)
    text = " | ".join(shown)
    if extra > 0:
        text += f" | +{extra} more"
    return text

def get_usb_device_status():
    ls = run_cmd(["lsusb"], 4)
    usb_lines = ls["stdout"].splitlines() if ls["ok"] and ls["stdout"] else []

    qmi = usb_glob("cdc-wdm*")
    serial = usb_glob("ttyUSB*") + usb_glob("ttyACM*")
    hidraw = usb_glob("hidraw*")
    storage = get_usb_storage_devices()

    sdr_lines = [
        line for line in usb_lines
        if re.search(r"(RTL283|RTL2838|Realtek|NooElec|DVB-T|R820T|SDR)", line, re.I)
    ]
    modem_lines = [
        line for line in usb_lines
        if re.search(r"(Telit|Sierra|Quectel|Qualcomm|Fibocom|SIMCom|Novatel|Huawei|LTE|Modem)", line, re.I)
    ]

    rows = [
        {
            "device": "QMI modem",
            "status": "DETECTED" if qmi else "NOT FOUND",
            "details": ", ".join(qmi) if qmi else "No /dev/cdc-wdm* device found",
        },
        {
            "device": "4G/LTE USB modem",
            "status": "DETECTED" if modem_lines else "UNKNOWN",
            "details": summarize_lsusb(modem_lines) if modem_lines else "No obvious modem match in lsusb",
        },
        {
            "device": "RTL-SDR / ADS-B",
            "status": "DETECTED" if sdr_lines else "NOT FOUND",
            "details": summarize_lsusb(sdr_lines) if sdr_lines else "No RTL-SDR / ADS-B receiver match in lsusb",
        },
        {
            "device": "USB serial",
            "status": "DETECTED" if serial else "NOT FOUND",
            "details": ", ".join(serial) if serial else "No /dev/ttyUSB* or /dev/ttyACM* devices found",
        },
        {
            "device": "USB storage",
            "status": "DETECTED" if storage else "NOT FOUND",
            "details": " | ".join(storage) if storage else "No USB storage devices shown by lsblk",
        },
        {
            "device": "USB bus",
            "status": f"{len(usb_lines)} DEVICE(S)" if usb_lines else "UNKNOWN",
            "details": summarize_lsusb(usb_lines),
        },
    ]

    return rows



def get_dir_size_bytes(path):
    try:
        path = Path(path)
        if not path.exists():
            return 0
        total = 0
        for p in path.rglob("*"):
            try:
                if p.is_file():
                    total += p.stat().st_size
            except Exception:
                pass
        return total
    except Exception:
        return 0

def get_diagnostics_status():
    size_bytes = get_dir_size_bytes(DIAG_DIR)
    try:
        file_count = sum(1 for p in Path(DIAG_DIR).glob("*") if p.is_file())
    except Exception:
        file_count = 0
    return {"folder": str(DIAG_DIR), "size_bytes": size_bytes, "size_mb": round(size_bytes / (1024 * 1024), 2), "file_count": file_count}

def clear_diagnostics_folder():
    removed = 0
    freed = 0
    try:
        DIAG_DIR.mkdir(parents=True, exist_ok=True)
        for p in DIAG_DIR.iterdir():
            try:
                if p.is_file() or p.is_symlink():
                    try: freed += p.stat().st_size
                    except Exception: pass
                    p.unlink()
                    removed += 1
                elif p.is_dir():
                    freed += get_dir_size_bytes(p)
                    shutil.rmtree(p)
                    removed += 1
            except Exception:
                pass
        return True, f"Cleared {removed} diagnostics item(s), freed {round(freed/(1024*1024),2)} MB."
    except Exception as e:
        return False, f"Failed to clear diagnostics: {e}"

def get_status():
    return {'timestamp':int(time.time()),'hostname':run_cmd(['hostname'],2)['stdout'],'interfaces':get_interfaces(),'wifi':get_wifi_info(),'internet':get_internet_status(),'lte':get_lte_status(),'system_health':get_system_health(),'power':get_power_runtime(),'network_latency':get_network_latency(),'usb_devices':get_usb_device_status(),'diagnostics':get_diagnostics_status(),'zerotier_devices':get_zerotier_connectivity(),'services':get_services(),'required_services':effective_required_services(),'warning_settings':warning_settings(),'warning_labels':WARNING_LABELS,'config':load_runtime_config(),'cpu':get_cpu_info()}

INDEX_HTML=r'''<!doctype html><html lang="en"><head><meta charset="utf-8"><title>TAK Server Dash</title><meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover"><style>
:root{--bg:#07090c;--panel:#121820;--text:#e8edf2;--muted:#8ea0ad;--good:#39d98a;--warn:#ffcc66;--bad:#ff5f57;--accent:#ff6a2a;--line:#26313d}*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at top,#17212b,var(--bg) 45%);color:var(--text);font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}header{padding:18px 18px 8px;border-bottom:1px solid var(--line);background:rgba(0,0,0,.25);position:sticky;top:0;backdrop-filter:blur(8px);z-index:2}h1{margin:0;font-size:1.35rem;letter-spacing:.08em;text-transform:uppercase}.subtitle{margin-top:4px;color:var(--muted);font-size:.9rem}.header-row{display:flex;align-items:center;justify-content:space-between;gap:14px}.privacy-toggle{white-space:nowrap;background:#1d2833;color:var(--text);border:1px solid #354556;border-radius:999px;padding:8px 14px;cursor:pointer;font-weight:750}.privacy-toggle:hover{border-color:var(--accent);color:#fff}body.privacy .sensitive{filter:blur(6px);user-select:none;transition:filter .15s ease}body.privacy .sensitive:hover{filter:blur(3px)}.section-toggle-grid{display:flex;flex-wrap:wrap;gap:8px 14px;margin:4px 0 8px}.section-toggle-grid label{display:inline-flex;align-items:center;gap:6px;border:1px solid var(--line);border-radius:999px;padding:6px 10px;background:rgba(255,255,255,.025);font-size:.86rem;color:var(--text)}.section-toggle-grid input{accent-color:var(--accent)}main{padding:14px;display:grid;grid-template-columns:repeat(12,1fr);gap:12px}.card{background:linear-gradient(180deg,rgba(255,255,255,.04),rgba(255,255,255,.015));border:1px solid var(--line);border-radius:14px;padding:14px;box-shadow:0 10px 30px rgba(0,0,0,.25)}.card h2{margin:0 0 10px;font-size:.95rem;letter-spacing:.06em;text-transform:uppercase}.span-4{grid-column:span 4}.span-6{grid-column:span 6}.span-12{grid-column:span 12}.kv{display:grid;grid-template-columns:150px 1fr;gap:6px 10px;font-size:.92rem}.k{color:var(--muted)}.v{word-break:break-word}.status{display:inline-flex;align-items:center;gap:6px;border:1px solid var(--line);border-radius:999px;padding:4px 9px;font-size:.82rem;text-transform:uppercase}.dot{width:9px;height:9px;border-radius:50%;background:var(--muted);display:inline-block}.good .dot{background:var(--good);box-shadow:0 0 8px var(--good)}.warn .dot{background:var(--warn);box-shadow:0 0 8px var(--warn)}.bad .dot{background:var(--bad);box-shadow:0 0 8px var(--bad)}.good{color:var(--good)}.warn{color:var(--warn)}.bad{color:var(--bad)}.wifi-not-connected{font-size:.95rem;font-weight:750;letter-spacing:.02em;text-transform:none}table{width:100%;border-collapse:collapse;font-size:.9rem}th,td{text-align:left;border-bottom:1px solid var(--line);padding:8px 6px;vertical-align:middle}th{color:var(--muted);font-size:.78rem;text-transform:uppercase;letter-spacing:.06em}button{background:#1d2833;color:var(--text);border:1px solid #354556;border-radius:9px;padding:7px 10px;cursor:pointer;margin:2px;font-weight:650}button.restart{border:2px solid rgba(255,204,102,.9);background:rgba(255,204,102,.14)}button.restart:hover{background:rgba(255,204,102,.22)}button.stop{border:2px solid rgba(255,95,87,.9);background:rgba(255,95,87,.14)}button.stop:hover{background:rgba(255,95,87,.22)}button:hover{border-color:var(--accent);color:#fff}button.start{border-color:rgba(57,217,138,.45)}button.stop{border-color:rgba(255,95,87,.45)}button.restart{border-color:rgba(255,204,102,.45)}button.primary{background:linear-gradient(180deg,#ff7a33,#b84618);border-color:#ff8b4d;color:white}pre{white-space:pre-wrap;background:#070a0f;border:1px solid var(--line);border-radius:10px;padding:10px;color:#d7e0e7;max-height:260px;overflow:auto}.small{font-size:.82rem;color:var(--muted)}.big{font-size:2rem;font-weight:750}.cpu-temp-value{font-size:1.05rem;font-weight:700}.bandwidth-value{font-size:1.05rem;font-weight:700}.zt-online{color:var(--good);font-weight:750}.zt-offline{color:var(--bad);font-weight:750}.zt-unknown{color:var(--warn);font-weight:750}@media(max-width:980px){main{grid-template-columns:1fr}.span-4,.span-6,.span-12{grid-column:span 1}.kv{grid-template-columns:120px 1fr}.header-row{align-items:flex-start}.privacy-toggle{padding:7px 10px;font-size:.82rem}}
html{-webkit-text-size-adjust:100%;text-size-adjust:100%}body{overflow-x:hidden}.card{min-width:0;overflow-x:auto}table{min-width:0}.section-controls table{min-width:0}.section-toggle-grid{max-height:160px;overflow-y:auto;-webkit-overflow-scrolling:touch}.privacy-toggle,button{touch-action:manipulation}@media(max-width:700px){.privacy-mode-text{font-size:.68rem;padding:5px 7px}header{padding:12px 10px 8px}.header-row{display:flex;align-items:flex-start;justify-content:space-between;gap:8px}h1{font-size:1rem;letter-spacing:.045em;line-height:1.2}.subtitle{font-size:.78rem}.privacy-toggle{padding:7px 9px;font-size:.78rem;max-width:118px;white-space:normal;line-height:1.1}main{padding:8px;gap:8px}.card{border-radius:11px;padding:10px}.card h2{font-size:.84rem;margin-bottom:8px}.kv{grid-template-columns:105px minmax(0,1fr);gap:5px 8px;font-size:.84rem}table{font-size:.78rem;min-width:0}th,td{padding:6px 5px}button{padding:8px 9px;margin:2px;font-size:.8rem}.section-toggle-grid{gap:6px;max-height:130px}.section-toggle-grid label{font-size:.78rem;padding:6px 8px}.big{font-size:1.08rem}.cpu-temp-value,.bandwidth-value{font-size:.98rem}}@media(max-width:420px){.header-row{gap:6px}h1{font-size:.92rem}.subtitle{font-size:.72rem}.privacy-toggle{font-size:.72rem;max-width:96px;padding:6px 7px}.kv{grid-template-columns:92px minmax(0,1fr);font-size:.8rem}table{font-size:.74rem;min-width:0}th,td{padding:5px 4px}.card{padding:9px}}@media(max-width:700px){html,body{width:100%;max-width:100%;overflow-x:hidden}main{width:100%;max-width:100%;overflow-x:hidden}.card{width:100%;max-width:100%;overflow-x:hidden}table{display:block;width:100%;min-width:0!important;max-width:100%;overflow:visible}thead{display:none}tbody{display:block;width:100%}tr{display:block;width:100%;border:1px solid var(--line);border-radius:10px;margin:0 0 8px;padding:5px;background:rgba(255,255,255,.018)}td{display:grid;grid-template-columns:88px minmax(0,1fr);gap:6px;align-items:start;width:100%;border-bottom:1px solid rgba(255,255,255,.06);padding:6px 4px;word-break:break-word;overflow-wrap:anywhere}td:last-child{border-bottom:0}td::before{content:attr(data-label);color:var(--muted);font-size:.68rem;text-transform:uppercase;letter-spacing:.05em;line-height:1.25}td:not([data-label])::before{content:''}.actions-cell{display:block}.actions-cell::before{display:block;margin-bottom:4px}.actions-cell button{display:inline-block;margin:2px 3px 4px 0;min-width:68px}.section-toggle-grid{display:grid;grid-template-columns:1fr 1fr;gap:6px;max-height:none;overflow:visible}.section-toggle-grid label{white-space:normal;line-height:1.2}}@media(max-width:420px){td{grid-template-columns:78px minmax(0,1fr);font-size:.74rem}.actions-cell button{min-width:62px;padding:7px 7px;font-size:.72rem}.section-toggle-grid{grid-template-columns:1fr}}.privacy-mode-text{font-size:.78rem;font-weight:800;letter-spacing:.08em;text-transform:uppercase;color:var(--good);border:1px solid rgba(57,217,138,.5);border-radius:999px;padding:6px 10px;background:rgba(57,217,138,.1);white-space:nowrap}body:not(.privacy) .privacy-mode-text{color:var(--accent);border-color:rgba(255,106,42,.5);background:rgba(255,106,42,.1)}.section-controls{padding:8px 12px;overflow:visible;position:relative}.section-details{position:relative}.section-details summary{cursor:pointer;display:inline-flex;align-items:center;gap:10px;list-style:none;border:1px solid var(--line);border-radius:999px;padding:7px 12px;background:rgba(255,255,255,.03);font-weight:800;text-transform:uppercase;letter-spacing:.05em;font-size:.78rem;user-select:none}.section-details summary::-webkit-details-marker{display:none}.section-details summary:after{content:'▾';color:var(--accent);font-size:.9rem}.section-details[open] summary:after{content:'▴'}.section-summary{color:var(--muted);font-weight:700;text-transform:none;letter-spacing:0;font-size:.76rem}.section-menu{position:absolute;z-index:40;top:calc(100% + 8px);left:0;width:min(560px,calc(100vw - 32px));background:#111922;border:1px solid var(--line);border-radius:14px;padding:10px;box-shadow:0 18px 40px rgba(0,0,0,.5)}.section-menu .section-toggle-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;max-height:min(52vh,420px);overflow-y:auto;-webkit-overflow-scrolling:touch}.section-menu .section-toggle-grid label{margin:0;white-space:normal;line-height:1.2}@media(max-width:700px){.section-controls{padding:7px 10px}.section-details summary{font-size:.74rem;padding:7px 10px}.section-menu{position:static;width:100%;max-width:100%;margin-top:8px;padding:8px;box-shadow:none}.section-menu .section-toggle-grid{grid-template-columns:1fr;max-height:45vh;overflow-y:auto}.section-summary{font-size:.7rem}}body{scroll-padding-top:64px}@media(max-width:700px){header{padding-top:64px!important}.mode-cluster .privacy-toggle{padding:6px 7px!important;font-size:.7rem!important;max-width:92px!important}.mode-cluster .privacy-mode-text{font-size:.66rem!important;padding:5px 7px!important}}#floatingModeCluster{position:fixed!important;top:10px!important;right:12px!important;left:auto!important;bottom:auto!important;z-index:2147483647!important;display:flex!important;align-items:center!important;justify-content:flex-end!important;gap:10px!important;padding:6px!important;margin:0!important;border:1px solid rgba(255,255,255,.08)!important;border-radius:16px!important;background:rgba(10,16,22,.92)!important;box-shadow:0 10px 28px rgba(0,0,0,.46)!important;backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);max-width:calc(100vw - 24px)!important;transform:none!important;}.privacy-mode-text{font-size:.78rem;font-weight:800;letter-spacing:.08em;text-transform:uppercase;color:var(--good);border:1px solid rgba(57,217,138,.5);border-radius:999px;padding:6px 10px;background:rgba(57,217,138,.1);white-space:nowrap}body:not(.privacy) .privacy-mode-text{color:var(--accent);border-color:rgba(255,106,42,.5);background:rgba(255,106,42,.1)}body{scroll-padding-top:70px}@media(max-width:700px){header{padding-top:64px!important}#floatingModeCluster{top:max(8px,env(safe-area-inset-top))!important;right:8px!important;bottom:auto!important;left:auto!important;gap:6px!important;padding:5px!important;border-radius:13px!important;max-width:calc(100vw - 16px)!important;}#floatingModeCluster .privacy-toggle{padding:6px 7px!important;font-size:.7rem!important;max-width:92px!important}#floatingModeCluster .privacy-mode-text{font-size:.66rem!important;padding:5px 7px!important}body{scroll-padding-top:70px}}.alert-banner{display:none;margin:12px 14px 0;padding:10px 12px;border:2px solid rgba(255,204,102,.75);border-radius:14px;background:rgba(255,204,102,.12);box-shadow:0 10px 30px rgba(0,0,0,.28);position:sticky;top:62px;z-index:5}.alert-banner.active{display:block}.alert-banner.critical{border-color:rgba(255,95,87,.82);background:rgba(255,95,87,.13)}.alert-title{font-weight:900;letter-spacing:.08em;text-transform:uppercase;font-size:.84rem;margin-bottom:7px}.alert-items{display:flex;flex-wrap:wrap;gap:7px}.alert-pill{display:inline-flex;align-items:center;border-radius:999px;border:1px solid rgba(255,204,102,.7);background:rgba(255,204,102,.12);color:var(--warn);padding:5px 9px;font-weight:800;font-size:.8rem;letter-spacing:.03em}.alert-pill.warn{border-color:rgba(255,204,102,.78);background:rgba(255,204,102,.14);color:var(--warn)}.alert-pill.bad{border-color:rgba(255,95,87,.78);background:rgba(255,95,87,.14);color:var(--bad)}@media(max-width:700px){.alert-banner{margin:8px 8px 0;top:58px;padding:9px}.alert-items{gap:6px}.alert-pill{font-size:.72rem;padding:5px 7px}}.section-head{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:10px}.section-head h2{margin:0}.inline-config{display:flex;flex-wrap:wrap;align-items:center;justify-content:flex-end;gap:7px}.inline-config input{background:#101820;color:var(--text);border:1px solid var(--line);border-radius:999px;padding:7px 10px;min-width:135px;max-width:190px}.mini-remove{padding:5px 7px!important;font-size:.72rem!important}.warning-toggle-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;max-height:min(52vh,420px);overflow-y:auto;-webkit-overflow-scrolling:touch}.warning-toggle-grid label{display:inline-flex;align-items:center;gap:7px;border:1px solid var(--line);border-radius:999px;padding:7px 10px;background:rgba(255,255,255,.025);font-size:.82rem}.warning-toggle-grid input{accent-color:var(--accent)}@media(max-width:700px){.section-head{align-items:stretch;flex-direction:column}.inline-config{justify-content:flex-start}.inline-config input{min-width:0;flex:1 1 130px}.warning-toggle-grid{grid-template-columns:1fr;max-height:45vh}}</style></head><body class="privacy"><div id="floatingModeCluster" class="mode-cluster"><span id="privacyModeText" class="privacy-mode-text">MODE: PRIVACY</span><button id="privacyToggle" class="privacy-toggle" onclick="togglePrivacyMode()">Normal Mode</button></div><header><div class="header-row"><div><h1>TAK Server Dash</h1><div class="subtitle" id="topline">Loading status…</div></div></div></header><div id="alertBanner" class="alert-banner" aria-live="polite"></div><main><section class="card span-12 section-controls" id="sectionControlsCard"><details class="section-details" id="sectionDetails"><summary><span>Dashboard Sections</span><span class="section-summary" id="sectionSummary">All visible</span></summary><div class="section-menu"><div class="section-toggle-grid" id="sectionToggleGrid"></div></div></details></section>
<section class="card span-4"><h2>Internet</h2><div id="internetBadge" class="status"><span class="dot"></span><span>Loading</span></div><div class="kv" style="margin-top:10px"><div class="k">IP ping</div><div class="v" id="pingIp">—</div><div class="k">DNS lookup</div><div class="v" id="dnsLookup">—</div><div class="k">Interface</div><div class="v" id="netIface">—</div><div class="k">Live download</div><div class="v bandwidth-value" id="netDown">—</div><div class="k">Live upload</div><div class="v bandwidth-value" id="netUp">—</div></div></section>
<section class="card span-4"><h2>4G/LTE</h2><div class="kv"><div class="k">State</div><div class="v" id="lteState">—</div><div class="k">Bars</div><div class="v big" id="lteBars">—</div><div class="k">Signal</div><div class="v" id="lteSignal">—</div><div class="k">Tech</div><div class="v" id="lteTech">—</div><div class="k">Operator</div><div class="v" id="lteOperator">—</div></div><div class="small" id="lteNote"></div></section>
<section class="card span-4"><h2>CPU Temp</h2><div class="kv"><div class="k">Current</div><div class="v cpu-temp-value" id="cpuCurrent">—</div><div class="k">24h high</div><div class="v cpu-temp-value" id="cpuHigh">—</div><div class="k">24h avg</div><div class="v" id="cpuAvg">—</div><div class="k">24h low</div><div class="v" id="cpuLow">—</div><div class="k">CPU freq.</div><div class="v" id="cpuFreq">—</div><div class="k">Samples</div><div class="v" id="cpuSamples">—</div></div><div style="height:1em"></div><div class="small">24h stats begin collecting after dashboard install/start.</div></section>
<section class="card span-6"><h2>System Health</h2><div class="kv"><div class="k">Uptime</div><div class="v" id="healthUptime">—</div><div class="k">Boot time</div><div class="v" id="healthBoot">—</div><div class="k">CPU load</div><div class="v" id="healthLoad">—</div><div class="k">RAM</div><div class="v" id="healthRam">—</div><div class="k">Disk</div><div class="v" id="healthDisk">—</div>
</div></section>
<section class="card span-6"><h2>Power / Runtime</h2><div class="kv"><div class="k">Status</div><div class="v" id="powerStatus">—</div><div class="k">Battery</div><div class="v" id="powerBattery">—</div><div class="k">Voltage</div><div class="v" id="powerVoltage">—</div><div class="k power-current-row">Current</div><div class="v power-current-row" id="powerCurrent">—</div><div class="k power-runtime-row">Runtime est.</div><div class="v power-runtime-row" id="powerRuntime">—</div><div class="k">Source</div><div class="v" id="powerSource">—</div></div><div class="small" id="powerDetails"></div></section>
<section class="card span-12"><h2>Network Latency</h2><table><thead><tr><th>Target</th><th>Host/IP</th><th>Status</th><th>Latency</th><th>Note</th></tr></thead><tbody id="latRows"></tbody></table></section>
<section class="card span-6"><h2>Interfaces</h2><table><thead><tr><th>Interface</th><th>State</th><th>IPv4</th></tr></thead><tbody id="ifaceRows"></tbody></table></section>
<section class="card span-6"><h2>Wi-Fi / HaLow Connection</h2><div class="kv"><div class="k">SSID</div><div class="v sensitive" id="wifiSsid">—</div><div class="k">Device</div><div class="v">wlan0</div><div class="k">IP address</div><div class="v sensitive" id="wifiIp">—</div><div class="k">Subnet mask</div><div class="v sensitive" id="wifiMask">—</div><div class="k">Gateway</div><div class="v sensitive" id="wifiGateway">—</div><div class="k">WiFi reception</div><div class="v"><span class="big" id="wifiBars">—</span> <span id="wifiSignal"></span></div></div></section>
<section class="card span-12"><h2>USB Device Status</h2><table><thead><tr><th>Device</th><th>Status</th><th>Details</th></tr></thead><tbody id="usbRows"></tbody></table></section>
<section class="card span-12 warning-controls" id="warningControlsCard"><details class="section-details"><summary><span>Banner Warnings</span><span class="section-summary" id="warningSummary">Default warnings</span></summary><div class="section-menu"><div class="warning-toggle-grid" id="warningToggleGrid"></div></div></details></section><section class="card span-12"><div class="section-head"><h2>ZeroTier Status</h2><div class="inline-config"><input id="ztIpInput" placeholder="Peer IP"><input id="ztCallsignInput" placeholder="Callsign"><button class="primary" onclick="addZtPeer()">Add Peer</button></div></div><table><thead><tr><th>IP</th><th>Callsign</th><th>Status</th><th>Last active</th><th>Actions</th></tr></thead><tbody id="ztRows"></tbody></table></section>
<section class="card span-12"><div class="section-head"><h2>Services</h2><div class="inline-config"><input id="serviceInput" placeholder="systemd service"><button class="primary" onclick="addServiceMonitor()">Add Service</button></div></div><table><thead><tr><th>Service</th><th>Load</th><th>Status</th><th>Substate</th><th>Enabled</th><th>Actions</th></tr></thead><tbody id="svcRows"></tbody></table></section>
<section class="card span-6"><h2>Backup / Diagnostics</h2><p class="small">Creates a timestamped diagnostics bundle on the TAK server with routes, interfaces, DNS, service status, and recent logs.</p><div class="kv" style="margin:8px 0"><div class="k">Folder size</div><div class="v" id="diagSize">—</div><div class="k">Files</div><div class="v" id="diagFiles">—</div></div><button class="primary" onclick="runDiagnostics()">Generate Diagnostics Bundle</button><button class="stop" onclick="clearDiagnostics()">Clear Old Diagnostics</button></section>
<section class="card span-6"><h2>TAK Server Power Controls</h2><p class="small">These buttons send commands to the Raspberry Pi TAK server. They do not reboot or shut down the computer viewing this webpage.</p><button class="restart" onclick="systemAction('reboot')">Reboot TAK Server</button><button class="stop" onclick="systemAction('shutdown')">Shutdown TAK Server</button></section>
<section class="card span-6"><h2>4G Modem DHCP Renew</h2><p class="small">Runs: <code>sudo dhclient -v wwan0</code> through a root-owned allowlist wrapper.</p><button class="primary" onclick="runDhclient()">Run dhclient -v wwan0</button></section>
<section class="card span-6"><h2>Last Command Output</h2><pre id="output">No command run yet.</pre></section>
</main><script>function fmtLoad(v){const n=Number(v);return Number.isFinite(n)?n.toFixed(1):'0.0'}function statusClassGeneric(s){s=String(s||'').toUpperCase();if(s.includes('DISCONNECTED')||s.includes('NO CLIENTS AVAILABLE')||s.includes('UNKNOWN')||s.includes('MODEM DETECTED'))return'warn';if(s.includes('OFFLINE')||s.includes('ERROR')||s.includes('FAIL')||s.includes('NOT FOUND'))return'bad';if(s.includes('ONLINE')||s.includes('CONNECTED')||s.includes('DETECTED')||s.includes('DEVICE')||s.includes('ACTIVE'))return'good';return'warn'}function interfaceStateClass(s){s=String(s||'').toLowerCase();if(s==='connected'||s.includes('connected')||s==='up'||s==='link-up')return'good';if(s==='down'||s.includes('down')||s.includes('not detected'))return'bad';return'warn'}function signalPercentClass(p){p=parseFloat(p);if(isNaN(p))return'';if(p<=40)return'bad';if(p<=70)return'warn';return'good'}function wifiSignalClass(p){return signalPercentClass(p)}
async function fetchStatus(){try{const r=await fetch('/api/status',{cache:'no-store'});if(!r.ok)throw new Error('HTTP '+r.status);render(await r.json())}catch(e){document.getElementById('topline').textContent='Dashboard error: '+e}}
function boolText(v){return v?'PASS':'FAIL'}function clsForInternet(s){if(s==='UP')return'status good';if((s||'').includes('DNS'))return'status warn';return'status bad'}function clsForSvc(a,l){if(l!=='loaded')return'bad';if(a==='active')return'good';if(a==='failed')return'bad';return'warn'}function esc(x){return String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function formatLastActive(ts){if(!ts)return'never';const d=new Date(ts*1000);const s=Math.max(0,Math.floor((Date.now()-d.getTime())/1000));let age=s<60?`${s}s ago`:s<3600?`${Math.floor(s/60)}m ago`:s<86400?`${Math.floor(s/3600)}h ago`:`${Math.floor(s/86400)}d ago`;return `${age} · ${d.toLocaleString()}`}
function ztStatusClass(i){if(i.status==='ONLINE')return'zt-online';if(i.status==='OFFLINE')return'zt-offline';return'zt-unknown'}

let lastStatusPayload=null;let lastStatusRenderMs=0;function pctNumber(v){const n=Number(v);return Number.isFinite(n)?n:null}
function networkConnected(s){
  const inet=s.internet||{};
  if(String(inet.state||'').toUpperCase()==='UP')return true;
  const ifs=Array.isArray(s.interfaces)?s.interfaces:[];
  if(ifs.some(i=>{
    const ips=Array.isArray(i.ipv4)?i.ipv4:[];
    const st=String(i.operstate||'').toLowerCase();
    return ips.length>0 && !st.includes('down') && !st.includes('not detected');
  }))return true;
  const wifi=s.wifi||{};
  if(wifi.ip_address)return true;
  const lte=s.lte||{};
  const ls=String(lte.state||'').toLowerCase();
  if(ls.includes('connected')||ls.includes('ipv4'))return true;
  return false;
}
function computeAlerts(s){
  const alerts=[];
  const add=(level,text)=>alerts.push({level,text});
  const ws=s.warning_settings||{};
  const enabled=k=>ws[k]!==false;
  if(enabled('network')&&!networkConnected(s))add('bad','NO NETWORK CONNECTED');
  const required=s.required_services||['opentakserver','eud_handler_ssl','rabbitmq-server'];
  const sv=s.services||{};
  required.forEach(name=>{
    const svc=sv[name]||Object.values(sv).find(x=>x&&x.name===name);
    if(enabled('services')&&(!svc||String(svc.active||'').toLowerCase()!=='active'))add('bad',`${name} NOT ACTIVE`);
  });
  const temp=pctNumber((s.cpu||{}).current_c);
  if(enabled('cpu_temp')&&temp!==null&&temp>65)add('bad',`CPU TEMP ${temp} °C`);
  const ltePct=pctNumber((s.lte||{}).signal_quality_percent);
  if(enabled('lte_reception')&&ltePct!==null&&ltePct<40)add('bad',`4G RECEPTION ${ltePct}%`);
  const batt=pctNumber((s.power||{}).battery_percent);
  if(enabled('battery')&&batt!==null&&batt<30)add('bad',`BATTERY ${batt}%`);
  const diskPct=pctNumber(((s.system_health||{}).disk||{}).used_percent);
  if(enabled('disk')&&diskPct!==null&&diskPct>=90)add('bad',`ROOT DISK CRITICAL ${diskPct}%`);
  else if(enabled('disk')&&diskPct!==null&&diskPct>=40)add('warn',`ROOT DISK ${diskPct}%`);
  const diagMb=pctNumber((s.diagnostics||{}).size_mb);
  if(enabled('diagnostics')&&diagMb!==null&&diagMb>300)add('warn',`DIAGNOSTICS ${diagMb} MB`);
  return alerts;
}
function renderAlerts(s){
  const banner=document.getElementById('alertBanner');
  if(!banner)return;
  const alerts=computeAlerts(s);
  const ws=s.warning_settings||{};
  if(ws.stale!==false&&lastStatusRenderMs&&Date.now()-lastStatusRenderMs>60000){
    const age=Math.floor((Date.now()-lastStatusRenderMs)/1000);
    alerts.push({level:'warn',text:`DASHBOARD DATA STALE ${age}s`});
  }
  if(!alerts.length){banner.className='alert-banner';banner.innerHTML='';return}
  const critical=alerts.some(a=>a.level==='bad');
  banner.className='alert-banner active '+(critical?'critical':'');
  banner.innerHTML=`<div class="alert-title">${critical?'SYSTEM WARNING':'SYSTEM NOTICE'}</div><div class="alert-items">${alerts.map(a=>`<span class="alert-pill ${esc(a.level)}">${esc(a.text)}</span>`).join('')}</div>`;
}

async function configAction(data){
  const r=await fetch('/api/config',{method:'POST',body:new URLSearchParams(data)});
  const j=await r.json().catch(()=>({ok:false,error:'Bad response'}));
  if(!j.ok){alert(j.error||'Config update failed');return}
  fetchStatus();
}
function addZtPeer(){
  const ip=document.getElementById('ztIpInput').value.trim();
  const callsign=document.getElementById('ztCallsignInput').value.trim();
  if(!ip){alert('Enter a ZeroTier peer IP/host first.');return}
  document.getElementById('ztIpInput').value='';
  document.getElementById('ztCallsignInput').value='';
  configAction({action:'add_zt',ip,callsign});
}
function removeZtPeer(ip){
  if(!confirm(`Remove ZeroTier peer ${ip}?`))return;
  configAction({action:'remove_zt',ip});
}
function addServiceMonitor(){
  const service=document.getElementById('serviceInput').value.trim();
  if(!service){alert('Enter a systemd service name first.');return}
  document.getElementById('serviceInput').value='';
  configAction({action:'add_service',service,required:'true'});
}
function removeServiceMonitor(service){
  if(!confirm(`Remove ${service} from monitoring?`))return;
  configAction({action:'remove_service',service});
}
function warningEnabled(settings,key){return !settings||settings[key]!==false}
function setWarning(key,enabled){configAction({action:'set_warning',key,enabled:enabled?'true':'false'})}
function renderWarningToggles(s){
  const grid=document.getElementById('warningToggleGrid');
  if(!grid)return;
  const settings=s.warning_settings||{};
  const labels=s.warning_labels||{};
  const keys=['network','services','cpu_temp','lte_reception','battery','disk','stale','diagnostics'];
  grid.innerHTML=keys.map(k=>`<label><input type="checkbox" ${warningEnabled(settings,k)?'checked':''} onchange="setWarning('${esc(k)}',this.checked)"> ${esc(labels[k]||k)}</label>`).join('');
  const off=keys.filter(k=>!warningEnabled(settings,k)).length;
  const el=document.getElementById('warningSummary');
  if(el){el.textContent=off?`${off} disabled`:'All enabled'}
}
function render(s){lastStatusPayload=s;lastStatusRenderMs=Date.now();renderWarningToggles(s);const dt=new Date(s.timestamp*1000);document.getElementById('topline').textContent=`${s.hostname||'pi'} · ${dt.toLocaleString()} · auto-refresh every 5 sec`;const inet=s.internet||{};const ib=document.getElementById('internetBadge');ib.className=clsForInternet(inet.state||'DOWN');ib.innerHTML=`<span class="dot"></span><span>${esc(inet.state||'UNKNOWN')}</span>`;document.getElementById('pingIp').textContent=boolText(inet.ping_ip);document.getElementById('dnsLookup').textContent=boolText(inet.dns_lookup);const bw=inet.bandwidth||{};document.getElementById('netIface').textContent=bw.interface||'unknown';document.getElementById('netDown').textContent=bw.download||'unknown';document.getElementById('netUp').textContent=bw.upload||'unknown';
const l=s.lte||{};document.getElementById('lteState').textContent=l.state||'unknown';const lb=document.getElementById('lteBars');lb.textContent=l.bars&&l.bars.text?l.bars.text:'unknown';lb.className='big '+signalPercentClass(l.signal_quality_percent);document.getElementById('lteSignal').textContent=l.signal_quality_percent!=null?`${l.signal_quality_percent}%${l.signal_dbm!=null?' / '+l.signal_dbm+' dBm':''}`:'unknown';document.getElementById('lteTech').textContent=l.access_technology?String(l.access_technology).toUpperCase():'UNKNOWN';document.getElementById('lteOperator').textContent=l.operator||'unknown';document.getElementById('lteNote').textContent='';
const c=s.cpu||{};document.getElementById('cpuCurrent').textContent=c.current_c==null?'—':`${c.current_c} °C`;document.getElementById('cpuHigh').textContent=c.highest_24h_c==null?'—':`${c.highest_24h_c} °C`;document.getElementById('cpuAvg').textContent=c.average_24h_c==null?'—':`${c.average_24h_c} °C`;document.getElementById('cpuLow').textContent=c.lowest_24h_c==null?'—':`${c.lowest_24h_c} °C`;document.getElementById('cpuFreq').textContent=c.frequency_mhz==null?'unknown':`${c.frequency_mhz} MHz`;document.getElementById('cpuSamples').textContent=c.samples_24h??'—';const h=s.system_health||{};document.getElementById('healthUptime').textContent=h.uptime||'unknown';document.getElementById('healthBoot').textContent=h.boot_time||'unknown';const health=s.system_health||{};const load=health.load_average||[];document.getElementById('healthLoad').textContent=load.length?`1m: ${fmtLoad(load[0])} / 5m: ${fmtLoad(load[1])} / 15m: ${fmtLoad(load[2])}`:'unknown';document.getElementById('healthRam').textContent=h.ram&&h.ram.text?h.ram.text:'unknown';document.getElementById('healthDisk').textContent=h.disk&&h.disk.text?h.disk.text:'unknown';const th=h.throttling||{};
const p=s.power||{};document.getElementById('powerStatus').textContent=p.status||'unknown';document.getElementById('powerBattery').textContent=p.battery_percent==null?'unknown':`${p.battery_percent}%`;document.getElementById('powerVoltage').textContent=p.battery_voltage_v==null?'unknown':`${p.battery_voltage_v} V`;const hasCurrent=p.current_a!=null;document.querySelectorAll('.power-current-row').forEach(e=>e.style.display=hasCurrent?'':'none');document.getElementById('powerCurrent').textContent=hasCurrent?`${p.current_a} A${p.power_w!=null?' / '+p.power_w+' W':''}`:'';const hasRuntime=!!p.runtime_estimate;document.querySelectorAll('.power-runtime-row').forEach(e=>e.style.display=hasRuntime?'':'none');document.getElementById('powerRuntime').textContent=hasRuntime?p.runtime_estimate:'';document.getElementById('powerSource').textContent=p.source||'unknown';document.getElementById('powerDetails').textContent=p.details||'';
const dg=s.diagnostics||{};document.getElementById('diagSize').textContent=dg.size_mb!=null?`${dg.size_mb} MB`:'unknown';document.getElementById('diagFiles').textContent=dg.file_count!=null?dg.file_count:'unknown';const lat=s.network_latency||[];document.getElementById('latRows').innerHTML=lat.map(i=>`<tr><td data-label="Target">${esc(i.name)}</td><td data-label="Host/IP"><span class="sensitive">${esc(i.host)}</span></td><td data-label="Status" class="${statusClassGeneric(i.status)}">${esc(i.status)}</td><td data-label="Latency">${i.latency_ms?esc(i.latency_ms)+' ms':'—'}</td><td data-label="Note">${esc(i.note||'')}</td></tr>`).join('');const ifs=s.interfaces||{};document.getElementById('ifaceRows').innerHTML=Object.values(ifs).map(i=>`<tr><td data-label="Interface">${esc(i.label||i.name)}</td><td data-label="State" class="${interfaceStateClass(i.operstate)}">${esc(String(i.operstate||'').toUpperCase())}</td><td data-label="IPv4"><span class="sensitive">${esc((i.ipv4||[]).join(', ')||'none')}</span></td></tr>`).join('');const w=s.wifi||{};document.getElementById('wifiSsid').textContent=w.ssid||'unknown';document.getElementById('wifiIp').textContent=w.ip_address||'unknown';document.getElementById('wifiMask').textContent=w.subnet_mask||'unknown';document.getElementById('wifiGateway').textContent=w.gateway||'unknown';const wb=document.getElementById('wifiBars');const wifiConnected=(w.connected===true)||!!w.ssid||!!w.ip_address;wb.textContent=wifiConnected?(w.bars&&w.bars.text?w.bars.text:'unknown'):'Not connected';wb.className=wifiConnected?('big '+wifiSignalClass(w.signal_percent)):'wifi-not-connected warn';document.getElementById('wifiSignal').textContent=(wifiConnected&&w.signal_percent!=null&&w.signal_percent!=='')?` ${w.signal_percent}%`:'';
const usb=s.usb_devices||[];document.getElementById('usbRows').innerHTML=usb.map(i=>`<tr><td data-label="Device">${esc(i.device)}</td><td data-label="Status" class="${statusClassGeneric(i.status)}">${esc(i.status)}</td><td data-label="Details">${esc(i.details||'')}</td></tr>`).join('');const z=s.zerotier_devices||[];document.getElementById('ztRows').innerHTML=z.length?z.map(i=>{const lat=i.latency_ms?` (${i.latency_ms} ms)`:'';return`<tr><td data-label="IP"><span class="sensitive">${esc(i.ip)}</span></td><td data-label="Callsign">${esc(i.callsign)}</td><td data-label="Status" class="${ztStatusClass(i)}">${esc(i.status)}${lat}</td><td data-label="Last active">${esc(formatLastActive(i.last_active_ts))}</td><td data-label="Actions" class="actions-cell"><button class="stop mini-remove" onclick="removeZtPeer('${esc(i.ip)}')">Remove</button></td></tr>`}).join(''):'<tr><td data-label="ZeroTier" colspan="5">No ZeroTier peers configured. Add one above or set TAK_DASHBOARD_ZEROTIER_DEVICES in /etc/tak-server-dash.env.</td></tr>';const sv=s.services||{};document.getElementById('svcRows').innerHTML=Object.values(sv).map(x=>`<tr><td data-label="Service"><b>${esc(x.name)}</b></td><td data-label="Load">${esc(x.load)}</td><td data-label="Status" class="${clsForSvc(x.active,x.load)}">${esc(String(x.active||'').toUpperCase())}</td><td data-label="Substate">${esc(x.sub)}</td><td data-label="Enabled">${esc(x.enabled)}</td><td data-label="Actions" class="actions-cell"><button class="start" onclick="svcAction('${esc(x.name)}','start')">Start</button><button class="stop" onclick="svcAction('${esc(x.name)}','stop')">Stop</button><button class="restart" onclick="svcAction('${esc(x.name)}','restart')">Restart</button><button class="stop mini-remove" onclick="removeServiceMonitor('${esc(x.name)}')">Remove</button></td></tr>`).join('');renderAlerts(s)}
async function postForm(url,data){const r=await fetch(url,{method:'POST',body:new URLSearchParams(data)});const j=await r.json();document.getElementById('output').textContent=(j.ok?'OK':'FAILED')+'\n\nCommand: '+(j.command||'')+'\nReturn code: '+(j.rc??'')+'\n\nSTDOUT:\n'+(j.stdout||'')+'\n\nSTDERR:\n'+(j.stderr||'');fetchStatus()}function svcAction(s,a){if(!confirm(`${a.toUpperCase()} ${s}?`))return;postForm('/api/service',{service:s,action:a})}function runDhclient(){if(!confirm('Run sudo dhclient -v wwan0 now?'))return;postForm('/api/dhclient',{})}async function runDiagnostics(){if(!confirm('Generate diagnostics bundle on the TAK server?'))return;const r=await fetch('/api/diagnostics',{method:'POST'});const j=await r.json();let t=(j.ok?'OK':'FAILED')+'\n\nCommand: '+(j.command||'')+'\nReturn code: '+(j.rc??'')+'\n\nSTDOUT:\n'+(j.stdout||'')+'\n\nSTDERR:\n'+(j.stderr||'');if(j.download_url){t+='\n\nDownload: '+j.download_url;window.location.href=j.download_url}document.getElementById('output').textContent=t;fetchStatus()}async function clearDiagnostics(){if(!confirm('Clear old diagnostics bundles on the TAK server?'))return;const r=await fetch('/api/diagnostics/clear',{method:'POST'});const t=await r.text();document.getElementById('output').textContent=t;fetchStatus()}function systemAction(action){const verb=action==='reboot'?'REBOOT':'SHUT DOWN';if(!confirm(`${verb} the Raspberry Pi TAK server now?\n\nThis affects the server running the dashboard, not the computer viewing this page.`))return;postForm('/api/system',{action})}function applyPrivacyMode(mode){const privateMode=mode==='privacy';document.body.classList.toggle('privacy',privateMode);const btn=document.getElementById('privacyToggle');if(btn){btn.textContent=privateMode?'Normal Mode':'Privacy Mode'}const modeText=document.getElementById('privacyModeText');if(modeText){modeText.textContent=privateMode?'MODE: PRIVACY':'MODE: NORMAL'}localStorage.setItem('takDashboardPrivacyMode',privateMode?'privacy':'normal')}function togglePrivacyMode(){const privateMode=document.body.classList.contains('privacy');applyPrivacyMode(privateMode?'normal':'privacy')}applyPrivacyMode('privacy');function sectionKey(title){return String(title||'').toLowerCase().replace(/[^a-z0-9]+/g,'-').replace(/^-|-$/g,'')}function getSectionPrefs(){try{return JSON.parse(localStorage.getItem('takDashboardSectionPrefs')||'{}')}catch(e){return {}}}function saveSectionPrefs(prefs){localStorage.setItem('takDashboardSectionPrefs',JSON.stringify(prefs))}function updateSectionSummary(){const sections=[...document.querySelectorAll('main > section.card')].filter(sec=>sec.id!=='sectionControlsCard');const hidden=sections.filter(sec=>sec.style.display==='none').length;const el=document.getElementById('sectionSummary');if(el){el.textContent=hidden?`${hidden} hidden`:'All visible'}}function setupSectionToggles(){const grid=document.getElementById('sectionToggleGrid');if(!grid)return;const prefs=getSectionPrefs();const sections=[...document.querySelectorAll('main > section.card')].filter(sec=>sec.id!=='sectionControlsCard');grid.innerHTML='';sections.forEach(sec=>{const h=sec.querySelector('h2');if(!h)return;const title=h.textContent.trim();const key=sectionKey(title);const checked=prefs[key]!==false;sec.style.display=checked?'':'none';const label=document.createElement('label');const cb=document.createElement('input');cb.type='checkbox';cb.checked=checked;cb.addEventListener('change',()=>{const p=getSectionPrefs();p[key]=cb.checked;saveSectionPrefs(p);sec.style.display=cb.checked?'':'none';updateSectionSummary()});label.appendChild(cb);label.appendChild(document.createTextNode(title));grid.appendChild(label)});updateSectionSummary()}document.addEventListener('click',e=>{const d=document.getElementById('sectionDetails');if(d&&d.open&&!d.contains(e.target)){d.open=false}});setupSectionToggles();fetchStatus();setInterval(fetchStatus,5000);setInterval(()=>{if(lastStatusPayload)renderAlerts(lastStatusPayload)},5000)
</script></body></html>'''

class Handler(BaseHTTPRequestHandler):
    server_version='TAKDashboard/1.5'
    def log_message(self,fmt,*args): print('%s - - [%s] %s'%(self.client_address[0],self.log_date_time_string(),fmt%args))
    def _auth_ok(self):
        return True

    def _require_auth(self):
        self.send_response(401); self.send_header('WWW-Authenticate','Basic realm="TAK Server Dash"'); self.send_header('Content-Type','text/plain'); self.end_headers(); self.wfile.write(b'Authentication required.\n')
    def _send_json(self,data,code=200):
        body=json.dumps(data,indent=2).encode(); self.send_response(code); self.send_header('Content-Type','application/json'); self.send_header('Cache-Control','no-store'); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)

    def _send_text(self, text, status=200):
        self.send_response(status); self.send_header('Content-Type','text/plain'); self.end_headers(); self.wfile.write(str(text).encode())

    def _handle_config_post(self):
        f=self._read_form()
        action=f.get('action','')
        cfg=load_runtime_config()

        if action=='add_zt':
            ip=sanitize_host_value(f.get('ip',''))
            callsign=str(f.get('callsign') or ip).strip()[:80]
            if not ip:
                return self._send_json({'ok':False,'error':'Invalid IP/host value'},400)
            peers=[p for p in cfg.get('zerotier_devices',[]) if p.get('ip')!=ip]
            peers.append({'ip':ip,'callsign':callsign or ip})
            cfg['zerotier_devices']=peers
            return self._send_json({'ok':True,'config':save_runtime_config(cfg)})

        if action=='remove_zt':
            ip=sanitize_host_value(f.get('ip',''))
            cfg['zerotier_devices']=[p for p in cfg.get('zerotier_devices',[]) if p.get('ip')!=ip]
            return self._send_json({'ok':True,'config':save_runtime_config(cfg)})

        if action=='add_service':
            svc=sanitize_service_name(f.get('service',''))
            if not svc:
                return self._send_json({'ok':False,'error':'Invalid service name'},400)
            services=cfg.get('services',[])
            if svc not in services:
                services.append(svc)
            cfg['services']=services
            req=cfg.get('required_services',[])
            if f.get('required','true').lower() in ('1','true','yes','on') and svc not in req:
                req.append(svc)
            cfg['required_services']=req
            return self._send_json({'ok':True,'config':save_runtime_config(cfg)})

        if action=='remove_service':
            svc=sanitize_service_name(f.get('service',''))
            cfg['services']=[s for s in cfg.get('services',[]) if s!=svc]
            cfg['required_services']=[s for s in cfg.get('required_services',[]) if s!=svc]
            return self._send_json({'ok':True,'config':save_runtime_config(cfg)})

        if action=='set_warning':
            key=str(f.get('key','')).strip()
            if key not in DEFAULT_WARNING_SETTINGS:
                return self._send_json({'ok':False,'error':'Invalid warning key'},400)
            enabled=str(f.get('enabled','')).lower() in ('1','true','yes','on')
            warnings=cfg.get('warning_settings',dict(DEFAULT_WARNING_SETTINGS))
            warnings[key]=enabled
            cfg['warning_settings']=warnings
            return self._send_json({'ok':True,'config':save_runtime_config(cfg)})

        return self._send_json({'ok':False,'error':'Invalid config action'},400)


    def _read_form(self):
        n=int(self.headers.get('Content-Length','0')); raw=self.rfile.read(n).decode() if n else ''; return {k:v[0] for k,v in parse_qs(raw).items()}
    def do_GET(self):
        if not self._auth_ok(): return self._require_auth()
        if self.path=='/' or self.path.startswith('/?'):
            body=INDEX_HTML.encode(); self.send_response(200); self.send_header('Content-Type','text/html; charset=utf-8'); self.send_header('Cache-Control','no-store'); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body); return
        if self.path=='/api/status': return self._send_json(get_status())
        if self.path=='/api/config': return self._send_json(load_runtime_config())
        if self.path.startswith('/diagnostics/'):
            fn=os.path.basename(self.path.split('/diagnostics/',1)[1].split('?',1)[0]); fp=DIAG_DIR/fn
            if not fp.exists() or not fp.is_file(): self.send_response(404); self.end_headers(); return
            body=fp.read_bytes(); self.send_response(200); self.send_header('Content-Type','application/gzip'); self.send_header('Content-Disposition',f'attachment; filename="{fn}"'); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body); return
        self.send_response(404); self.end_headers()
    def do_POST(self):
        if not self._auth_ok(): return self._require_auth()
        if self.path=='/api/config':
            return self._handle_config_post()
        if self.path=='/api/service':
            f=self._read_form(); svc=f.get('service',''); act=f.get('action','')
            if svc not in effective_services() or act not in ('start','stop','restart'): return self._send_json({'ok':False,'stderr':'Invalid service/action','rc':2},400)
            cmd=['sudo','/usr/local/sbin/tak-server-dash-action','service',act,svc]; r=run_cmd(cmd,45); r['command']=' '.join(cmd); return self._send_json(r)
        if self.path=='/api/dhclient':
            cmd=['sudo','/usr/local/sbin/tak-server-dash-action','dhclient']; r=run_cmd(cmd,90); r['command']='sudo dhclient -v wwan0'; return self._send_json(r)
        if self.path=='/api/diagnostics':
            cmd=['sudo','/usr/local/sbin/tak-server-dash-action','diagnostics']; r=run_cmd(cmd,90); r['command']='Generate diagnostics bundle on TAK server'
            if r['ok'] and r['stdout']: r['download_url']='/diagnostics/'+os.path.basename(r['stdout'].splitlines()[-1].strip())
            return self._send_json(r)
        if self.path=='/api/system':
            f=self._read_form(); act=f.get('action','')
            if act not in ('reboot','shutdown'): return self._send_json({'ok':False,'stderr':'Invalid system action','rc':2},400)
            cmd=['sudo','/usr/local/sbin/tak-server-dash-action','system',act]; r=run_cmd(cmd,15); r['command']=f'Schedule TAK server {act}'; return self._send_json(r)
        self.send_response(404); self.end_headers()

def main():
    DATA_DIR.mkdir(parents=True,exist_ok=True); DIAG_DIR.mkdir(parents=True,exist_ok=True)
    if not CONFIG_FILE.exists(): save_runtime_config(default_runtime_config())
    threading.Thread(target=temp_sampler_loop,daemon=True).start()
    httpd=ThreadingHTTPServer((BIND,PORT),Handler); print(f'TAK dashboard listening on http://{BIND}:{PORT}'); httpd.serve_forever()
if __name__=='__main__': main()
