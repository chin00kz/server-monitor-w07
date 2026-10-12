#!/usr/bin/env python3
"""Read-only host reliability checks. No service or network changes."""

import json
import shutil
import subprocess
import time

CONTAINERS = (
    "server-monitor",
    "imfs-pg-m1",
    "imfs-pg-m2",
    "imfs-pg-m3",
    "imfs-pg-m4",
    "imfs-pg-integration",
)


def collect_docker():
    result = {"status": "unknown", "containers": {}}
    try:
        proc = subprocess.run(
            ["docker", "inspect", "--type=container", *CONTAINERS],
            capture_output=True, text=True, timeout=10, check=True,
        )
        records = json.loads(proc.stdout)
        for record in records:
            name = record.get("Name", "").lstrip("/")
            if name not in CONTAINERS:
                continue
            state = record.get("State") or {}
            health = (state.get("Health") or {}).get("Status", "none")
            result["containers"][name] = {
                "state": state.get("Status", "unknown"),
                "health": health,
                "restarts": record.get("RestartCount", 0),
            }
        result["status"] = (
            "ok" if len(result["containers"]) == len(CONTAINERS)
            else "partial"
        )
    except (OSError, ValueError, subprocess.SubprocessError, TypeError):
        pass
    return result


def collect_storage():
    try:
        usage = shutil.disk_usage("/")
        total, used, free = usage.total, usage.used, usage.free
        percent = round(used * 100 / total, 1) if total else None
        return {
            "status": "ok",
            "mount": "/",
            "total_bytes": total,
            "used_bytes": used,
            "available_bytes": free,
            "used_percent": percent,
            "level": (
                "unknown" if percent is None else
                "critical" if percent >= 90 else
                "warning" if percent >= 80 else "normal"
            ),
        }
    except OSError:
        return {"status": "unknown"}


def collect_failover(network):
    preferred = network.get("preferred_interface")
    connectivity = network.get("connectivity") or {}
    starlink = (connectivity.get("starlink") or {}).get("status", "unknown")
    dialog = (connectivity.get("dialog") or {}).get("status", "unknown")

    if preferred == "wlp2s0":
        state = "primary_preferred"
    elif preferred == "enp1s0":
        state = "backup_preferred"
    else:
        state = "unknown"

    return {
        "status": state,
        "preferred_interface": preferred,
        "starlink": starlink,
        "dialog": dialog,
        "backup_internet_confirmed": (
            preferred == "enp1s0" and dialog == "reachable"
        ),
        "checked_at": int(time.time()),
    }
