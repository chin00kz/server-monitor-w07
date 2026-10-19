"""API reliability mapping for optional Ollama / partial docker snapshots."""

import json
import os
import sys
import time
import unittest
from unittest.mock import patch

APP_SRC = os.path.join(os.path.dirname(__file__), "..", "app", "src")
sys.path.insert(0, APP_SRC)

import main  # noqa: E402


def _ok_container():
    return {
        "state": "running",
        "health": "healthy",
        "restarts": 0,
        "started_at": "2026-10-14T10:00:00.123456789Z",
    }


def _snapshot_without_ollama():
    containers = {
        name: _ok_container()
        for name in (
            "server-monitor",
            "imfs-pg-m1",
            "imfs-pg-m2",
            "imfs-pg-m3",
            "imfs-pg-m4",
            "imfs-pg-integration",
            "homelab-test",
        )
    }
    return {
        "timestamp": int(time.time()),
        "docker": {"status": "partial", "containers": containers},
        "storage": {"status": "unknown"},
        "network": {
            "preferred_interface": "wlp2s0",
            "wifi_ssid": "starlink-mini",
            "connectivity": {
                "starlink": {"status": "reachable"},
                "dialog": {"status": "unknown"},
            },
            "interface_connectivity": {},
        },
    }


def _patch_snapshot(snapshot):
    payload = json.dumps(snapshot)
    original = main.Path.read_text

    def read_text(self, *args, **kwargs):
        parts = self.parts
        if parts[-2:] == ("activity", "latest.json") or (
            len(parts) >= 3 and parts[-3:] == ("data", "activity", "latest.json")
        ):
            return payload
        return original(self, *args, **kwargs)

    return patch.object(main.Path, "read_text", read_text)


class ReliabilityApiOllamaTests(unittest.TestCase):
    def test_partial_snapshot_keeps_other_containers_and_marks_ollama_unknown(self):
        with _patch_snapshot(_snapshot_without_ollama()):
            body = main.reliability()

        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["docker"]["status"], "partial")
        self.assertEqual(
            body["docker"]["containers"]["imfs-pg-m1"]["state"], "running"
        )
        self.assertEqual(
            body["docker"]["containers"]["homelab-ollama"]["state"], "unknown"
        )
        # Optional: not expected/critical unless env says so.
        self.assertFalse(
            body["docker"]["containers"]["homelab-ollama"]["expected_running"]
        )
        self.assertFalse(
            body["docker"]["containers"]["homelab-ollama"]["critical"]
        )

    def test_partial_status_is_preserved_not_collapsed_to_ok(self):
        with _patch_snapshot(_snapshot_without_ollama()):
            body = main.reliability()

        self.assertEqual(body["docker"]["status"], "partial")
        self.assertNotEqual(body["docker"]["status"], "ok")

    def test_full_ok_snapshot_preserves_ok_status(self):
        snap = _snapshot_without_ollama()
        snap["docker"]["containers"]["homelab-ollama"] = _ok_container()
        snap["docker"]["status"] = "ok"
        with _patch_snapshot(snap):
            body = main.reliability()

        self.assertEqual(body["docker"]["status"], "ok")
        self.assertEqual(
            body["docker"]["containers"]["homelab-ollama"]["state"], "running"
        )

    def test_failed_inspection_stays_unknown(self):
        snap = _snapshot_without_ollama()
        snap["docker"] = {"status": "unknown", "containers": {}}
        with _patch_snapshot(snap):
            body = main.reliability()

        self.assertEqual(body["docker"]["status"], "unknown")
        self.assertEqual(
            body["docker"]["containers"]["homelab-ollama"]["state"], "unknown"
        )


if __name__ == "__main__":
    unittest.main()
