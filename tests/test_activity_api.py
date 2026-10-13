import json
import sys
import tempfile
import unittest
from pathlib import Path


APP_SRC = Path(__file__).resolve().parents[1] / "app" / "src"
sys.path.insert(0, str(APP_SRC))

from activity_snapshot import ACTIVITY_DATABASES, load_activity_snapshot  # noqa: E402


NOW = 2_000_000_000


def database(status="ok", **overrides):
    item = {
        "status": status,
        "active": 1,
        "idle": 2,
        "idle_in_transaction": 3,
        "other": 4,
        "total": 10,
    }
    item.update(overrides)
    return item


def snapshot():
    return {
        "timestamp": NOW - 30,
        "databases": {
            name: database() for name in ACTIVITY_DATABASES
        },
        "sessions": {
            "ssh": {"status": "ok", "connections": 2},
            "local": {"status": "ok", "sessions": 1},
        },
    }


class ActivityApiTests(unittest.TestCase):
    def call_activity(self, payload=None, raw=None, missing=False):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "latest.json"
            if not missing:
                path.write_text(raw if raw is not None else json.dumps(payload))
            return load_activity_snapshot(path, NOW)

    def test_three_healthy_and_two_unavailable(self):
        data = snapshot()
        unavailable = ("imfs-pg-m2", "imfs-pg-integration")
        for name in unavailable:
            data["databases"][name] = database("unknown")

        _, timestamp, age, databases = self.call_activity(data)

        self.assertEqual(timestamp, NOW - 30)
        self.assertEqual(age, 30)
        for name in unavailable:
            self.assertEqual(databases[name], {"status": "unknown"})
        self.assertEqual(databases["imfs-pg-m1"]["total"], 10)

    def test_all_databases_healthy(self):
        _, _, _, databases = self.call_activity(snapshot())

        self.assertTrue(all(
            item["status"] == "ok"
            for item in databases.values()
        ))

    def test_all_databases_unavailable(self):
        data = snapshot()
        data["databases"] = {
            name: database("unknown") for name in ACTIVITY_DATABASES
        }

        source, _, _, databases = self.call_activity(data)

        self.assertTrue(all(
            item == {"status": "unknown"}
            for item in databases.values()
        ))
        self.assertEqual(source["sessions"]["ssh"]["status"], "ok")

    def test_missing_malformed_and_stale_snapshots_fail_closed(self):
        stale = snapshot()
        stale["timestamp"] = NOW - 91

        with self.assertRaises(OSError):
            self.call_activity(missing=True)
        with self.assertRaises(json.JSONDecodeError):
            self.call_activity(raw="not json")
        with self.assertRaises(ValueError):
            self.call_activity(stale)

    def test_invalid_counts_downgrade_only_that_database(self):
        data = snapshot()
        data["databases"]["imfs-pg-m3"]["total"] = -1

        source, _, _, databases = self.call_activity(data)

        self.assertEqual(
            databases["imfs-pg-m3"], {"status": "unknown"}
        )
        self.assertEqual(databases["imfs-pg-m4"]["status"], "ok")
        self.assertEqual(source["sessions"]["ssh"]["status"], "ok")

    def test_malformed_database_shape_rejects_snapshot(self):
        data = snapshot()
        data["databases"]["imfs-pg-m3"] = None

        with self.assertRaises(ValueError):
            self.call_activity(data)


if __name__ == "__main__":
    unittest.main()
