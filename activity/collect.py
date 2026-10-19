#!/usr/bin/env python3
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

from network_health import (
    ALL_INTERFACES,
    WAN_INTERFACES,
    isp_connectivity,
    network_status_ok,
)
from reliability import collect_docker, collect_storage, collect_failover

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "data" / "activity" / "latest.json"

DATABASES = (
    "imfs-pg-m1",
    "imfs-pg-m2",
    "imfs-pg-m3",
    "imfs-pg-m4",
    "imfs-pg-integration",
)

SQL = """
SELECT json_build_object(
    'active', count(*) FILTER (WHERE state = 'active'),
    'idle', count(*) FILTER (WHERE state = 'idle'),
    'idle_in_transaction',
        count(*) FILTER (WHERE state LIKE 'idle in transaction%'),
    'other', count(*) FILTER (
        WHERE state IS NULL
           OR state NOT IN ('active', 'idle')
              AND state NOT LIKE 'idle in transaction%'
    ),
    'total', count(*),
    'long_running', count(*) FILTER (
        WHERE state = 'active'
          AND query_start < now() - interval '60 seconds'
    ),
    'max_connections', current_setting('max_connections')::int,
    'database_size_bytes', pg_database_size(current_database())
)
FROM pg_stat_activity
WHERE backend_type = 'client backend'
  AND pid <> pg_backend_pid();
"""

def collect_database(name):
    command = [
        "docker", "exec", name,
        "sh", "-c",
        'PGOPTIONS="-c statement_timeout=3000" '
        'psql -X -q -t -A -v ON_ERROR_STOP=1 '
        '-U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "$1"',
        "sh", SQL,
    ]

    started = time.monotonic()

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=8,
            check=True,
        )
        stats = json.loads(result.stdout.strip())
        stats["response_ms"] = round(
            (time.monotonic() - started) * 1000
        )
        return {"status": "ok", **stats}
    except (subprocess.SubprocessError, ValueError, OSError):
        return {"status": "unknown"}


def collect_sessions():
    """Collect session counts only; do not store usernames or remote IPs."""
    result = {"ssh": {"status": "unknown", "connections": None},
              "local": {"status": "unknown", "sessions": None}}

    try:
        ssh = subprocess.run(
            ["ss", "-H", "-tn", "state", "established", "sport", "=", ":22"],
            capture_output=True, text=True, timeout=5, check=True,
        )
        result["ssh"] = {
            "status": "ok",
            "connections": sum(bool(line.strip()) for line in ssh.stdout.splitlines()),
        }
    except (subprocess.SubprocessError, OSError):
        pass

    try:
        local = subprocess.run(
            ["who"], capture_output=True, text=True, timeout=5, check=True,
        )
        result["local"] = {
            "status": "ok",
            "sessions": sum(bool(line.strip()) for line in local.stdout.splitlines()),
        }
    except (subprocess.SubprocessError, OSError):
        pass

    return result




def check_internet(interface):
    """Check HTTPS over a bound interface, reporting failures accurately."""
    result = {
        "status": "unknown",
        "http_code": None,
        "response_ms": None,
    }

    command = [
        "curl", "-4",
        "--interface", f"if!{interface}",
        "--noproxy", "*",
        "--connect-timeout", "3",
        "--max-time", "6",
        "--silent", "--show-error",
        "--output", "/dev/null",
        "--write-out", "%{http_code} %{time_total}",
        "https://www.cloudflare.com/cdn-cgi/trace",
    ]

    try:
        response = subprocess.run(
            command, capture_output=True, text=True,
            timeout=8, check=False,
        )
        parts = response.stdout.strip().split()
        if len(parts) == 2:
            code = int(parts[0])
            seconds = float(parts[1])
            result["http_code"] = code or None
            result["response_ms"] = round(seconds * 1000)

            if response.returncode == 0 and code == 200:
                result["status"] = "reachable"
            elif response.returncode == 0:
                result["status"] = "http_error"
            else:
                result["status"] = "unreachable"
        elif response.returncode != 0:
            result["status"] = "unreachable"
    except (subprocess.SubprocessError, OSError, ValueError, OverflowError):
        pass

    return result

def connected_wifi_ssid():
    """Read the actual connected Wi-Fi SSID using iw."""
    try:
        r = subprocess.run(
            ["iw", "dev", "wlp2s0", "link"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        if r.returncode != 0 or "Not connected." in r.stdout:
            return None

        for line in r.stdout.splitlines():
            line = line.strip()
            if line.startswith("SSID:"):
                return line.split(":", 1)[1].strip() or None

    except (OSError, subprocess.SubprocessError):
        pass

    return None


NETWORK_CACHE = ROOT / "data/activity/network-cache.json"
ACTIVE_INTERVAL = 300
BACKUP_INTERVAL = 1800


def collect_network(force_probe=False):
    """Refresh active network every 5m and backup connectivity every 30m, or on force_probe."""
    now = int(time.time())

    try:
        cache = json.loads(NETWORK_CACHE.read_text())
        if not isinstance(cache, dict):
            cache = {}
    except (OSError, ValueError):
        cache = {}

    last_active = cache.get("active_checked_at", 0)
    last_backup = cache.get("backup_checked_at", 0)

    # Route selection is cheap to inspect and must never be cached.
    # Connectivity probes retain their existing refresh intervals unless forced.
    current_preferred = None
    try:
        route_now = subprocess.run(
            ["ip", "-j", "-4", "route", "get", "1.1.1.1"],
            capture_output=True, text=True, timeout=5, check=True,
        )
        routes_now = json.loads(route_now.stdout)
        if routes_now and isinstance(routes_now[0], dict):
            current_preferred = routes_now[0].get("dev")
    except (subprocess.SubprocessError, ValueError, OSError, TypeError):
        pass

    cached_network = cache.get("network")
    previous_preferred = (
        cached_network.get("preferred_interface")
        if isinstance(cached_network, dict) else None
    )
    current_ssid = connected_wifi_ssid()
    ssid_changed = current_ssid != cache.get("wifi_ssid")
    cache["wifi_ssid"] = current_ssid

    route_changed = (
        current_preferred in WAN_INTERFACES
        and current_preferred != previous_preferred
    )

    # Allow fresh probe on manual refresh, but rate-limit to at most once per 10s to prevent flooding
    manual_probe_due = (
        force_probe
        and (
            not isinstance(last_active, (int, float))
            or now - last_active >= 10
        )
    )

    active_due = (
        manual_probe_due
        or route_changed
        or ssid_changed
        or not isinstance(last_active, (int, float))
        or last_active > now
        or now - last_active >= ACTIVE_INTERVAL
        or "network" not in cache
    )

    backup_due = (
        manual_probe_due
        or not isinstance(last_backup, (int, float))
        or last_backup > now
        or now - last_backup >= BACKUP_INTERVAL
        or "backup_result" not in cache
    )

    if active_due:
        network = {
            "status": "unknown",
            "preferred_interface": None,
            "interfaces": {},
        }

        for name in ALL_INTERFACES:
            network["interfaces"][name] = {
                "status": "unknown",
                "operstate": None,
                "ipv4": None,
            }

        try:
            addresses = subprocess.run(
                ["ip", "-j", "-4", "address", "show"],
                capture_output=True, text=True, timeout=5, check=True,
            )
            for iface in json.loads(addresses.stdout):
                name = iface.get("ifname")
                if name not in network["interfaces"]:
                    continue
                ipv4 = [
                    item.get("local")
                    for item in iface.get("addr_info", [])
                    if item.get("family") == "inet" and item.get("local")
                ]
                network["interfaces"][name] = {
                    "status": "ok",
                    "operstate": iface.get("operstate", "UNKNOWN"),
                    "ipv4": ipv4[0] if ipv4 else None,
                }
        except (subprocess.SubprocessError, ValueError, OSError, TypeError):
            pass

        try:
            route = subprocess.run(
                ["ip", "-j", "-4", "route", "get", "1.1.1.1"],
                capture_output=True, text=True, timeout=5, check=True,
            )
            routes = json.loads(route.stdout)
            if routes and isinstance(routes[0], dict):
                network["preferred_interface"] = routes[0].get("dev")
        except (subprocess.SubprocessError, ValueError, OSError, TypeError):
            pass

        # Overall status reflects preferred WAN inventory only — not Tailscale
        # and not internet reachability (that comes from check_internet probes).
        if network_status_ok(
            network["preferred_interface"], network["interfaces"]
        ):
            network["status"] = "ok"

        preferred = network["preferred_interface"]
        if preferred in WAN_INTERFACES:
            cache["active_result"] = check_internet(preferred)
        else:
            cache["active_result"] = {
                "status": "unknown", "http_code": None, "response_ms": None
            }

        cache["network"] = network
        cache["active_checked_at"] = now

    network = cache["network"]
    preferred = network.get("preferred_interface")
    backup = (
        "enp1s0" if preferred == "wlp2s0"
        else "wlp2s0" if preferred == "enp1s0"
        else None
    )

    if backup_due or cache.get("backup_interface") != backup:
        cache["backup_result"] = (
            check_internet(backup) if backup else
            {"status": "unknown", "http_code": None, "response_ms": None}
        )
        cache["backup_interface"] = backup
        cache["backup_checked_at"] = now

    unknown = {"status": "unknown", "http_code": None, "response_ms": None}

    wifi_result = (
        cache.get("active_result", unknown) if preferred == "wlp2s0"
        else cache.get("backup_result", unknown) if backup == "wlp2s0"
        else unknown
    )
    ethernet_result = (
        cache.get("active_result", unknown) if preferred == "enp1s0"
        else cache.get("backup_result", unknown) if backup == "enp1s0"
        else unknown
    )

    network["wifi_ssid"] = current_ssid
    # Raw per-interface probe results, independent of ISP labeling.
    network["interface_connectivity"] = {
        "wlp2s0": wifi_result,
        "enp1s0": ethernet_result,
    }
    # ISP labels require an explicit configured SSID (starlink-mini / Dialog).
    network["connectivity"] = isp_connectivity(
        current_ssid, wifi_result, ethernet_result
    )

    network["active_checked_at"] = cache.get("active_checked_at")
    network["backup_checked_at"] = cache.get("backup_checked_at")

    NETWORK_CACHE.parent.mkdir(parents=True, exist_ok=True)
    temporary = NETWORK_CACHE.with_suffix(".tmp")
    temporary.write_text(json.dumps(cache, separators=(",", ":")) + "\n")
    os.replace(temporary, NETWORK_CACHE)

    return network


LOCK_FILE = ROOT / "data/activity/.collect.lock"
TRIGGER_FILE = ROOT / "data/activity/refresh.request"
STATUS_FILE = ROOT / "data/activity/refresh.status"


def run_collection(request_id=None, force_probe=False):
    """Run full host collection protected by an exclusive file lock."""
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_FILE.open("w") as lock_fd:
        locked = False
        try:
            import fcntl
            deadline = time.monotonic() + 45.0
            while True:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        return None
                    time.sleep(0.25)
        except (ImportError, OSError):
            pass  # fcntl unavailable on Windows; proceed

        started = time.time()
        try:
            # If request_id was not explicitly passed, check if a trigger file is waiting
            if not request_id and TRIGGER_FILE.exists():
                try:
                    data = json.loads(TRIGGER_FILE.read_text())
                    req_id = data.get("request_id")
                    if isinstance(req_id, str) and req_id:
                        request_id = req_id
                        force_probe = True
                except (OSError, ValueError):
                    pass
                finally:
                    try:
                        TRIGGER_FILE.unlink(missing_ok=True)
                    except OSError:
                        pass

            snapshot = {
                "timestamp": int(time.time()),
                "request_id": request_id,
                "databases": {
                    name: collect_database(name)
                    for name in DATABASES
                },
                "sessions": collect_sessions(),
                "network": collect_network(force_probe=force_probe),
                "docker": collect_docker(),
                "storage": collect_storage(),
            }
            snapshot["failover"] = collect_failover(snapshot["network"])

            OUTPUT.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(
                prefix=".activity-",
                dir=OUTPUT.parent,
            )
            try:
                with os.fdopen(fd, "w") as file:
                    json.dump(snapshot, file, separators=(",", ":"))
                    file.write("\n")
                try:
                    os.chmod(temporary, 0o664)
                except OSError:
                    pass
                os.replace(temporary, OUTPUT)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)

            # Write status acknowledgment if this run fulfilled a request
            if request_id:
                STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
                temp_status = STATUS_FILE.with_suffix(".tmp")
                status_payload = {
                    "request_id": request_id,
                    "status": "completed",
                    "timestamp": snapshot["timestamp"],
                    "duration_seconds": round(time.time() - started, 2),
                }
                temp_status.write_text(json.dumps(status_payload) + "\n")
                try:
                    os.chmod(temp_status, 0o664)
                except OSError:
                    pass
                os.replace(temp_status, STATUS_FILE)

            return snapshot
        finally:
            try:
                if locked:
                    import fcntl
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except (ImportError, OSError):
                pass


def main():
    snapshot = run_collection()
    print(json.dumps(snapshot, indent=2))


if __name__ == "__main__":
    main()
