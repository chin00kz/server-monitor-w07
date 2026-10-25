import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1]))
import publisher_oracle as oracle


class OraclePublisherTests(unittest.TestCase):
    def test_health_state_mapping(self):
        self.assertEqual(oracle.health_state("running", "healthy"), "healthy")
        self.assertEqual(oracle.health_state("running", "none"), "no_check")
        self.assertEqual(oracle.health_state("exited", "none"), "stopped")
        self.assertEqual(oracle.health_state("missing", "none"), "unknown")

    def test_build_payload_healthy_backup(self):
        with patch.object(oracle, "inspect_container", return_value=("running", "healthy")), patch.object(
            oracle, "uptime_seconds", return_value=42.0
        ):
            payload = oracle.build_payload()
        self.assertEqual(payload["node"]["id"], oracle.NODE_ID)
        self.assertEqual(payload["services"]["total"], 1)
        item = payload["services"]["items"][0]
        self.assertEqual(item["name"], "IMFS Integration Backup")
        self.assertEqual(item["status"], "healthy")
        self.assertEqual(item["role"], "backup")
        oracle.validate_payload(payload)

    def test_build_payload_missing_container(self):
        with patch.object(oracle, "inspect_container", return_value=("unknown", "none")), patch.object(
            oracle, "uptime_seconds", return_value=None
        ):
            payload = oracle.build_payload()
        self.assertEqual(payload["services"]["items"][0]["status"], "unknown")
        self.assertEqual(payload["services"]["healthy"], 0)


if __name__ == "__main__":
    unittest.main()
