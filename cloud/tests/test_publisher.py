import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1]))
import publisher


class CloudPublisherTests(unittest.TestCase):
    def test_build_payload_reuses_snapshot_and_sqlite_metrics(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database_path = root / "metrics.db"
            snapshot_path = root / "latest.json"
            proc_path = root / "proc"
            proc_path.mkdir()
            (proc_path / "meminfo").write_text("MemTotal:       1000 kB\n")
            (proc_path / "uptime").write_text("123.5 0.0\n")
            database = sqlite3.connect(database_path)
            try:
                database.execute("create table s (key text, ts integer, v real)")
                database.executemany("insert into s values (?, ?, ?)", [
                    ("cpu.percent", 1, 12.5),
                    ("mem.used", 1, 500000),
                    ("bat.percent", 1, 80),
                    ("bat.ac", 1, 1),
                ])
                database.commit()
            finally:
                database.close()
            snapshot_path.write_text(json.dumps({
                "timestamp": 100,
                "network": {"preferred_interface": "wlp2s0", "connectivity": {
                    "starlink": {"status": "reachable"},
                    "dialog": {"status": "unreachable"},
                }},
                "storage": {"used_percent": 22},
                "docker": {"containers": {
                    "imfs-pg-m1": {"state": "running", "health": "healthy"},
                    "imfs-pg-m2": {"state": "running", "health": "none"},
                }},
            }))
            with patch.object(publisher, "DB_PATH", database_path), patch.object(
                publisher, "ACTIVITY_SNAPSHOT", snapshot_path
            ), patch.object(publisher, "PROC_ROOT", proc_path):
                payload = publisher.build_payload(publisher.read_json(snapshot_path))
            self.assertEqual(payload["network"]["active_route"], "starlink-mini")
            self.assertEqual(payload["resources"]["cpu_percent"], 12.5)
            self.assertEqual(payload["resources"]["disk_percent"], 22)
            self.assertEqual(payload["battery"]["activity"], "unknown")
            self.assertEqual(payload["services"]["healthy"], 1)
            self.assertEqual(payload["services"]["total"], 5)

    def test_publish_sends_only_status_record(self):
        payload = {"node": {"observed_at": 1}, "resources": {}, "services": {"total": 5}}
        with patch.dict(os.environ, {
            "SUPABASE_URL": "https://example.supabase.co",
            "SUPABASE_SERVICE_ROLE_KEY": "secret-for-test",
        }), patch.object(publisher.urllib.request, "urlopen") as urlopen:
            response = urlopen.return_value.__enter__.return_value
            response.status = 204
            publisher.publish(payload)
            request = urlopen.call_args.args[0]
            self.assertIn("/rest/v1/node_status?on_conflict=node_id", request.full_url)
            self.assertNotIn("secret-for-test", request.data.decode())


if __name__ == "__main__":
    unittest.main()
