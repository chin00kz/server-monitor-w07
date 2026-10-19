import os
import sys
import unittest

APP_SRC = os.path.join(os.path.dirname(__file__), "..", "app", "src")
sys.path.insert(0, APP_SRC)

from snapshot_sanitize import (  # noqa: E402
    sanitize_activity_database,
    sanitize_postgres_database,
)


def _ok_activity(**overrides):
    base = {
        "status": "ok",
        "active": 1,
        "idle": 2,
        "idle_in_transaction": 0,
        "other": 0,
        "total": 3,
    }
    base.update(overrides)
    return base


def _ok_postgres(**overrides):
    base = {
        "status": "ok",
        "active": 1,
        "idle": 2,
        "idle_in_transaction": 0,
        "other": 0,
        "total": 3,
        "long_running": 0,
        "max_connections": 100,
        "database_size_bytes": 1024,
        "response_ms": 5,
    }
    base.update(overrides)
    return base


class ActivitySanitizeTests(unittest.TestCase):
    def test_healthy_entry_preserves_count_contract_without_status(self):
        result = sanitize_activity_database(_ok_activity())
        self.assertEqual(
            result,
            {
                "active": 1,
                "idle": 2,
                "idle_in_transaction": 0,
                "other": 0,
                "total": 3,
            },
        )
        self.assertNotIn("status", result)

    def test_unhealthy_database_degrades_without_raising(self):
        self.assertEqual(
            sanitize_activity_database({"status": "error"}),
            {"status": "unknown"},
        )

    def test_invalid_count_degrades_without_raising(self):
        self.assertEqual(
            sanitize_activity_database(_ok_activity(total=-1)),
            {"status": "unknown"},
        )


class PostgresSanitizeTests(unittest.TestCase):
    def test_healthy_entry_keeps_ok_contract(self):
        roles = {"expected_running": True, "critical": True}
        result = sanitize_postgres_database(_ok_postgres(), roles)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["total"], 3)
        self.assertTrue(result["critical"])

    def test_invalid_metric_degrades_instead_of_failing_all(self):
        roles = {"expected_running": False, "critical": False}
        result = sanitize_postgres_database(
            _ok_postgres(response_ms="slow"), roles
        )
        self.assertEqual(result, {"status": "unknown", **roles})

    def test_zero_max_connections_degrades(self):
        roles = {"expected_running": True, "critical": False}
        result = sanitize_postgres_database(
            _ok_postgres(max_connections=0), roles
        )
        self.assertEqual(result["status"], "unknown")


if __name__ == "__main__":
    unittest.main()
