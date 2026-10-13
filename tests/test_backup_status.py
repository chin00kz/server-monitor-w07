import json
import sys
import tempfile
import unittest
from pathlib import Path


APP_SRC = Path(__file__).resolve().parents[1] / "app" / "src"
sys.path.insert(0, str(APP_SRC))

from backup_status import load_backup_status  # noqa: E402


NOW = 2_000_000_000


class BackupStatusTests(unittest.TestCase):
    def write(self, value):
        directory = tempfile.TemporaryDirectory()
        path = Path(directory.name) / "status.json"
        path.write_text(json.dumps(value))
        self.addCleanup(directory.cleanup)
        return path

    def test_valid_status_preserves_backup_and_storage_readings(self):
        path = self.write({
            "status": "successful",
            "timestamp": NOW - 30,
            "message": "Backup completed",
            "last_local": {"status": "successful", "timestamp": NOW - 60},
            "last_cloud": {"status": "successful", "timestamp": NOW - 50},
            "storage": {"status": "ok", "total": 1000, "used": 400, "free": 600},
        })

        result = load_backup_status(path, NOW)

        self.assertEqual(result["status"], "successful")
        self.assertEqual(result["last_local"]["timestamp"], NOW - 60)
        self.assertEqual(result["last_cloud"]["status"], "successful")
        self.assertEqual(result["storage"]["free"], 600)

    def test_missing_malformed_and_stale_status_fail_closed(self):
        missing = Path(tempfile.gettempdir()) / "server-monitor-missing-backup-status.json"
        malformed = self.write(["not", "an", "object"])
        stale = self.write({"status": "successful", "timestamp": NOW - 181})

        for path in (missing, malformed, stale):
            with self.subTest(path=path):
                self.assertEqual(load_backup_status(path, NOW)["status"], "unavailable")

    def test_cloud_unavailability_does_not_hide_local_success(self):
        path = self.write({
            "status": "unavailable",
            "timestamp": NOW,
            "message": "Cloud unavailable",
            "last_local": {"status": "successful", "timestamp": NOW - 10},
            "last_cloud": {"status": "unavailable", "timestamp": None},
            "storage": {"status": "unavailable"},
        })

        result = load_backup_status(path, NOW)

        self.assertEqual(result["last_local"]["status"], "successful")
        self.assertEqual(result["last_cloud"]["status"], "unavailable")
        self.assertEqual(result["storage"]["status"], "unavailable")


if __name__ == "__main__":
    unittest.main()
