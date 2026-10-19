"""Endpoint-level regression tests for partial PostgreSQL degradation.

Exercises the FastAPI route handlers for /api/activity and /api/postgres/health
with mocked host snapshots. Does not touch live databases or start the app
lifespan/collector thread.
"""

import json
import os
import sys
import time
import unittest
from unittest.mock import patch

APP_SRC = os.path.join(os.path.dirname(__file__), "..", "app", "src")
sys.path.insert(0, APP_SRC)

import main  # noqa: E402

EXPECTED_DBS = (
    "imfs-pg-m1",
    "imfs-pg-m2",
    "imfs-pg-m3",
    "imfs-pg-m4",
    "imfs-pg-integration",
)


def _ok_activity_db():
    return {
        "status": "ok",
        "active": 1,
        "idle": 2,
        "idle_in_transaction": 0,
        "other": 0,
        "total": 3,
        "long_running": 0,
        "max_connections": 100,
        "database_size_bytes": 2048,
        "response_ms": 7,
    }


def _snapshot(degraded=None):
    databases = {name: _ok_activity_db() for name in EXPECTED_DBS}
    if degraded:
        databases[degraded] = {"status": "error"}
    return {
        "timestamp": int(time.time()),
        "databases": databases,
        "sessions": {
            "ssh": {"status": "ok", "connections": 0},
            "local": {"status": "ok", "sessions": 0},
        },
        "network": {"status": "unknown"},
    }


def _patch_snapshot(snapshot):
    payload = json.dumps(snapshot)
    original = main.Path.read_text

    def read_text(self, *args, **kwargs):
        # Match container path on POSIX and Windows Path string forms.
        parts = self.parts
        if parts[-2:] == ("activity", "latest.json") or (
            len(parts) >= 3 and parts[-3:] == ("data", "activity", "latest.json")
        ):
            return payload
        return original(self, *args, **kwargs)

    return patch.object(main.Path, "read_text", read_text)


class ActivityEndpointPartialFailureTests(unittest.TestCase):
    def test_activity_keeps_healthy_databases_when_one_is_degraded(self):
        degraded = "imfs-pg-m2"
        with _patch_snapshot(_snapshot(degraded=degraded)):
            body = main.activity()

        self.assertEqual(body["status"], "ok")
        self.assertEqual(set(body["databases"]), set(EXPECTED_DBS))
        self.assertEqual(body["databases"][degraded], {"status": "unknown"})
        healthy = body["databases"]["imfs-pg-m1"]
        self.assertNotIn("status", healthy)
        self.assertEqual(healthy["total"], 3)
        self.assertEqual(healthy["active"], 1)

    def test_postgres_health_keeps_healthy_databases_when_one_is_degraded(self):
        degraded = "imfs-pg-integration"
        with _patch_snapshot(_snapshot(degraded=degraded)):
            body = main.postgres_health()

        self.assertEqual(body["status"], "ok")
        self.assertEqual(set(body["databases"]), set(EXPECTED_DBS))
        self.assertEqual(body["databases"][degraded]["status"], "unknown")
        healthy = body["databases"]["imfs-pg-m1"]
        self.assertEqual(healthy["status"], "ok")
        self.assertEqual(healthy["total"], 3)
        self.assertEqual(healthy["max_connections"], 100)


if __name__ == "__main__":
    unittest.main()
