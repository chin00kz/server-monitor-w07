import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ACTIVITY = Path(__file__).resolve().parents[1] / "activity"
sys.path.insert(0, str(ACTIVITY))

import reliability  # noqa: E402


class DockerReliabilityTests(unittest.TestCase):
    @patch("reliability.subprocess.run")
    def test_collect_docker_includes_started_at_without_extra_commands(self, run):
        run.return_value.stdout = json.dumps([
            {
                "Name": f"/{name}",
                "State": {
                    "Status": "running",
                    "StartedAt": "2026-10-14T10:00:00.123456789Z",
                    "Health": {"Status": "healthy"},
                },
                "RestartCount": 2,
            }
            for name in reliability.CONTAINERS
        ])

        result = reliability.collect_docker()

        self.assertEqual(result["status"], "ok")
        self.assertEqual(
            result["containers"]["server-monitor"]["started_at"],
            "2026-10-14T10:00:00.123456789Z",
        )
        self.assertEqual(result["containers"]["server-monitor"]["restarts"], 2)
        run.assert_called_once_with(
            ["docker", "inspect", "--type=container", *reliability.CONTAINERS],
            capture_output=True, text=True, timeout=10, check=True,
        )

    @patch("reliability.subprocess.run")
    def test_missing_optional_docker_fields_remain_unavailable(self, run):
        run.return_value.stdout = json.dumps([{
            "Name": "/server-monitor",
            "State": {"Status": "exited"},
        }])

        item = reliability.collect_docker()["containers"]["server-monitor"]

        self.assertIsNone(item["started_at"])
        self.assertIsNone(item["restarts"])


if __name__ == "__main__":
    unittest.main()
