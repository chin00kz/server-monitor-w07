import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


ACTIVITY = Path(__file__).resolve().parents[1] / "activity"
sys.path.insert(0, str(ACTIVITY))

import reliability  # noqa: E402


def _inspect_payload(name, status="running", health="healthy", restarts=2):
    return json.dumps([{
        "Name": f"/{name}",
        "State": {
            "Status": status,
            "StartedAt": "2026-10-14T10:00:00.123456789Z",
            "Health": {"Status": health},
        },
        "RestartCount": restarts,
    }])


class DockerReliabilityTests(unittest.TestCase):
    @patch("reliability.subprocess.run")
    def test_collect_docker_includes_started_at_without_extra_commands(self, run):
        run.side_effect = [
            Mock(stdout=_inspect_payload(name))
            for name in reliability.CONTAINERS
        ]

        result = reliability.collect_docker()

        self.assertEqual(result["status"], "ok")
        self.assertIn("homelab-ollama", reliability.CONTAINERS)
        self.assertEqual(
            result["containers"]["server-monitor"]["started_at"],
            "2026-10-14T10:00:00.123456789Z",
        )
        self.assertEqual(result["containers"]["server-monitor"]["restarts"], 2)
        self.assertEqual(run.call_count, len(reliability.CONTAINERS))
        run.assert_any_call(
            ["docker", "inspect", "--type=container", "server-monitor"],
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

    @patch("reliability.subprocess.run")
    def test_missing_ollama_does_not_drop_other_containers(self, run):
        def fake_run(cmd, **kwargs):
            name = cmd[-1]
            if name == "homelab-ollama":
                raise subprocess.CalledProcessError(1, cmd)
            return Mock(stdout=_inspect_payload(name))

        run.side_effect = fake_run
        result = reliability.collect_docker()

        self.assertEqual(result["status"], "partial")
        self.assertNotIn("homelab-ollama", result["containers"])
        self.assertIn("server-monitor", result["containers"])
        self.assertIn("imfs-pg-m1", result["containers"])
        self.assertEqual(
            result["containers"]["server-monitor"]["state"], "running"
        )


if __name__ == "__main__":
    unittest.main()
