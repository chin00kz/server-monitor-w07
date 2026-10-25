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
    "homelab-test",
    "homelab-ollama",
)


def _container_record(record):
    state = record.get("State") or {}
    health = (state.get("Health") or {}).get("Status", "none")
    return {
        "state": state.get("Status", "unknown"),
        "health": health,
        "restarts": record.get("RestartCount"),
        "started_at": state.get("StartedAt"),
    }


def collect_docker():
    """Inspect each monitored container independently.

    A missing optional container (for example homelab-ollama) must not discard
    results for the rest of the fleet. Batch inspect fails entirely when any
    name is absent, so probes are per-container.
    """
    result = {"status": "unknown", "containers": {}}
    for name in CONTAINERS:
        try:
            proc = subprocess.run(
                ["docker", "inspect", "--type=container", name],
                capture_output=True, text=True, timeout=10, check=True,
            )
            records = json.loads(proc.stdout)
            if not records:
                continue
            result["containers"][name] = _container_record(records[0])
        except (OSError, ValueError, subprocess.SubprocessError, TypeError, IndexError):
            continue

    if not result["containers"]:
        return result
    result["status"] = (
        "ok" if len(result["containers"]) == len(CONTAINERS)
        else "partial"
    )
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
