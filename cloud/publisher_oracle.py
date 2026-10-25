#!/usr/bin/env python3
"""Publish Oracle host Postgres backup status to Supabase (allowlisted container only)."""

import json
import os
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

NODE_ID = os.environ.get("CLOUD_NODE_ID", "ubsever02")
TABLE = os.environ.get("SUPABASE_STATUS_TABLE", "node_status")
MAX_PAYLOAD_BYTES = 8 * 1024
PROC_ROOT = Path(os.environ.get("HOST_PROC", "/proc"))
CONTAINER_NAME = os.environ.get("ORACLE_PG_CONTAINER", "imfs-pg-integration")
SERVICE_LABEL = "IMFS Integration Backup"
DOCKER_BIN = os.environ.get("DOCKER_BIN", "docker")


def uptime_seconds():
    try:
        return float((PROC_ROOT / "uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def health_state(state, health):
    state = str(state or "unknown").lower()
    health = str(health or "none").lower()
    if state == "running" and health == "healthy":
        return "healthy"
    if state == "running" and health == "none":
        return "no_check"
    if state == "running" and health == "starting":
        return "starting"
    if state == "running" and health == "unhealthy":
        return "unhealthy"
    if state in {"exited", "stopped", "dead", "created"}:
        return "stopped"
    return "unknown"


def inspect_container(name):
    """Return (state, health) for one allowlisted container name."""
    try:
        output = subprocess.check_output(
            [
                DOCKER_BIN,
                "inspect",
                "--format",
                "{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}",
                name,
            ],
            text=True,
            timeout=8,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return "unknown", "none"
    if "|" not in output:
        return "unknown", "none"
    state, _, health = output.partition("|")
    return state.strip() or "unknown", (health.strip() or "none")


def build_payload():
    state, health = inspect_container(CONTAINER_NAME)
    status = health_state(state, health)
    observed = datetime.now(timezone.utc).isoformat()
    return {
        "node": {
            "id": NODE_ID,
            "uptime_seconds": uptime_seconds(),
            "observed_at": observed,
        },
        "network": {
            "active_route": "unknown",
            "starlink": "unknown",
            "dialog": "unknown",
        },
        "battery": {
            "activity": "unknown",
            "percent": None,
            "on_ac": None,
            "status": "unknown",
        },
        "resources": {
            "cpu_percent": None,
            "ram_percent": None,
            "disk_percent": None,
        },
        "services": {
            "items": [{"name": SERVICE_LABEL, "status": status, "role": "backup"}],
            "healthy": 1 if status == "healthy" else 0,
            "total": 1,
        },
    }


def validate_payload(payload):
    encoded = json.dumps(payload, separators=(",", ":")).encode()
    if len(encoded) > MAX_PAYLOAD_BYTES:
        raise ValueError("payload exceeds size limit")
    items = (payload.get("services") or {}).get("items") or []
    if len(items) != 1 or items[0].get("name") != SERVICE_LABEL:
        raise ValueError("invalid oracle service payload")
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
    publish(validate_payload(build_payload()))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyError, OSError, ValueError, RuntimeError, urllib.error.URLError) as error:
        print(f"oracle cloud publisher failed: {type(error).__name__}: {error}")
        raise SystemExit(1)
