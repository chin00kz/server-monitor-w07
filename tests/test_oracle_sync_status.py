import json
import sys
import tempfile
import unittest
from pathlib import Path


APP_SRC = Path(__file__).resolve().parents[1] / "app" / "src"
sys.path.insert(0, str(APP_SRC))

from oracle_sync_status import load_oracle_sync_status  # noqa: E402


NOW = 2_000_000_000
HASH_A = "a" * 64
HASH_B = "b" * 64


class OracleSyncStatusTests(unittest.TestCase):
    def write(self, value, log_text="line one\n"):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        status = root / "oracle-sync-status.json"
        log = root / "oracle-sync.log"
        status.write_text(json.dumps(value))
        log.write_text(log_text)
        return status, log

    def test_awaiting_confirmation_exposes_hashes_and_log(self):
        status, log = self.write({
            "status": "awaiting_confirmation",
            "timestamp": NOW - 10,
            "message": "Ready for confirmation",
            "request_id": "a" * 32,
            "dump_hashes": {
                "imfs_central": HASH_A,
                "imfs_tenant_apex": HASH_B,
                "evil": "c" * 64,
            },
            "started_at": NOW - 100,
            "finished_at": NOW - 20,
            "duration_seconds": 80,
            "exit_status": 0,
        })

        result = load_oracle_sync_status(status, log, NOW, live_enabled=False)

        self.assertEqual(result["status"], "awaiting_confirmation")
        self.assertEqual(result["request_id"], "a" * 32)
        self.assertEqual(result["dump_hashes"], {
            "imfs_central": HASH_A,
            "imfs_tenant_apex": HASH_B,
        })
        self.assertEqual(result["log"], "line one\n")
        self.assertFalse(result["live_enabled"])

    def test_stale_and_malformed_fail_closed(self):
        missing = Path(tempfile.gettempdir()) / "missing-oracle-sync-status.json"
        status, log = self.write(["not", "object"])
        stale, stale_log = self.write({
            "status": "successful",
            "timestamp": NOW - 90_000,
            "message": "old",
        })

        for path, log_path in (
            (missing, Path(tempfile.gettempdir()) / "missing-oracle.log"),
            (status, log),
            (stale, stale_log),
        ):
            with self.subTest(path=path):
                self.assertEqual(
                    load_oracle_sync_status(path, log_path, NOW)["status"],
                    "unavailable",
                )


if __name__ == "__main__":
    unittest.main()
