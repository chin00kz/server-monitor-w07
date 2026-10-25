#!/usr/bin/env python3
"""Publish an approved read-only status snapshot to Supabase."""

import json
import os
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

NODE_ID = os.environ.get("CLOUD_NODE_ID", "workstation-07-5d")
ACTIVITY_SNAPSHOT = Path(os.environ.get("ACTIVITY_SNAPSHOT", "data/activity/latest.json"))
DB_PATH = Path(os.environ.get("DB_PATH", "data/metrics.db"))
PROC_ROOT = Path(os.environ.get("HOST_PROC", "/proc"))
TABLE = os.environ.get("SUPABASE_STATUS_TABLE", "node_status")
MAX_PAYLOAD_BYTES = 48 * 1024
SERVICE_NAMES = (
    ("imfs-pg-integration", "IMFS Integration"),
    ("imfs-pg-m1", "IMFS M1"),
    ("imfs-pg-m2", "IMFS M2"),
    ("imfs-pg-m3", "IMFS M3"),
    ("imfs-pg-m4", "IMFS M4"),
    ("homelab-ollama", "Ollama"),
)


def read_json(path):
    with path.open(encoding="utf-8") as source:
        value = json.load(source)
    if not isinstance(value, dict):
        raise ValueError("snapshot must be an object")
    return value


def latest_metric(key):
    if not DB_PATH.exists():
        return None
    database = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=3)
    try:
        row = database.execute(
            "SELECT v FROM s WHERE key = ? ORDER BY ts DESC LIMIT 1", (key,)
        ).fetchone()
    finally:
        database.close()
    return float(row[0]) if row else None


def memory_total_bytes():
    try:
        for line in (PROC_ROOT / "meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return float(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def uptime_seconds():
    try:
        return float((PROC_ROOT / "uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def health_state(item):
    if not isinstance(item, dict):
        return "unknown"
    state = str(item.get("state") or "unknown").lower()
    health = str(item.get("health") or "none").lower()
    if state == "running" and health == "healthy":
        return "healthy"
    if state == "running" and health == "none":
        return "no_check"
    if state == "running" and health == "starting":
        return "starting"
    if state == "running" and health == "unhealthy":
        return "unhealthy"
    if state in {"exited", "stopped", "dead"}:
        return "stopped"
    return "unknown"


def reachability_state(value):
    return value if value in {"reachable", "unreachable"} else "unknown"


def build_payload(snapshot):
    network = snapshot.get("network") or {}
    connectivity = network.get("connectivity") or {}
    preferred = network.get("preferred_interface")
    route = (
        "starlink-mini" if preferred == "wlp2s0" else
        "dialog-ethernet" if preferred == "enp1s0" else "unknown"
    )
    docker = snapshot.get("docker") or {}
    raw_containers = docker.get("containers") or {}
    services = []
    for container, label in SERVICE_NAMES:
        item = raw_containers.get(container)
        state = health_state(item)
        services.append({"name": label, "status": state})
    resources = {
        "cpu_percent": latest_metric("cpu.percent"),
        "ram_percent": None,
        "disk_percent": (snapshot.get("storage") or {}).get("used_percent"),
    }
    memory_used = latest_metric("mem.used")
    memory_total = memory_total_bytes()
    if memory_used is not None and memory_total:
        resources["ram_percent"] = round(memory_used * 100 / memory_total, 1)
    battery_percent = latest_metric("bat.percent")
    battery_ac = latest_metric("bat.ac")
    return {
        "node": {
            "id": NODE_ID,
            "uptime_seconds": uptime_seconds(),
            "observed_at": datetime.fromtimestamp(
                float(snapshot["timestamp"]), timezone.utc
            ).isoformat(),
        },
        "network": {
            "active_route": route,
            "starlink": reachability_state((connectivity.get("starlink") or {}).get("status")),
            "dialog": reachability_state((connectivity.get("dialog") or {}).get("status")),
        },
        "battery": {
            "activity": "unknown",
            "percent": battery_percent,
            "on_ac": None if battery_ac is None else bool(battery_ac),
            "status": "unknown",
        },
        "resources": resources,
        "services": {
            "items": services,
            "healthy": sum(item["status"] == "healthy" for item in services),
            "total": len(services),
        },
    }


def validate_payload(payload):
    encoded = json.dumps(payload, separators=(",", ":")).encode()
    if len(encoded) > MAX_PAYLOAD_BYTES:
        raise ValueError("payload exceeds size limit")
    resources = payload.get("resources") or {}
    for key in ("cpu_percent", "ram_percent", "disk_percent"):
        value = resources.get(key)
        if value is not None and (not isinstance(value, (int, float)) or not 0 <= value <= 100):
            raise ValueError(f"invalid resource value: {key}")
    if payload.get("services", {}).get("total") != len(SERVICE_NAMES):
        raise ValueError("invalid service total")
    return payload


def publish(payload):
    base_url = os.environ["SUPABASE_URL"].rstrip("/")
    service_key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    record = {
        "node_id": NODE_ID,
        "observed_at": payload["node"].get("observed_at"),
        "payload": payload,
    }
    request = urllib.request.Request(
        f"{base_url}/rest/v1/{TABLE}?on_conflict=node_id",
        data=json.dumps(record, separators=(",", ":")).encode(),
        headers={
            "apikey": service_key,
            "Authorization": f"Bearer {service_key}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        if response.status >= 300:
            raise RuntimeError(f"Supabase publish failed: HTTP {response.status}")


def main():
    snapshot = read_json(ACTIVITY_SNAPSHOT)
    publish(validate_payload(build_payload(snapshot)))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyError, OSError, ValueError, RuntimeError, urllib.error.URLError) as error:
        print(f"cloud publisher failed: {type(error).__name__}: {error}")
        raise SystemExit(1)
