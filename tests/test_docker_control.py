import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "activity"))

import docker_control


class DockerControlTests(unittest.TestCase):
    def test_default_allowlist_is_test_container_only(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(docker_control.configured_containers(), {"homelab-test"})

    def test_production_and_postgres_containers_are_rejected(self):
        for name in (
            "server-monitor",
            "imfs-pg-m1",
            "imfs-pg-m2",
            "imfs-pg-m3",
            "imfs-pg-m4",
            "imfs-pg-integration",
        ):
            with self.subTest(container=name):
                with self.assertRaises(PermissionError):
                    docker_control.validate_request({
                        "request_id": "a" * 32,
                        "container": name,
                        "action": "stop",
                    })

    def test_only_fixed_actions_are_accepted_for_test_container(self):
        for action in ("start", "stop", "restart", "logs"):
            with self.subTest(action=action):
                result = docker_control.validate_request({
                    "request_id": "a" * 32,
                    "container": "homelab-test",
                    "action": action,
                })
                self.assertEqual(result[2], action)

        with self.assertRaises(ValueError):
            docker_control.validate_request({
                "request_id": "a" * 32,
                "container": "homelab-test",
                "action": "exec",
            })

    @patch.dict(os.environ, {"DOCKER_CONTROL_CONTAINERS": "homelab-test"})
    @patch.object(docker_control.subprocess, "run")
    def test_start_uses_fixed_docker_arguments(self, run):
        run.return_value.returncode = 0
        run.return_value.stdout = ""
        run.return_value.stderr = ""

        ok, result = docker_control.run_action("homelab-test", "start")

        self.assertTrue(ok)
        self.assertEqual(result["message"], "Docker start completed")
        self.assertEqual(
            run.call_args.args[0],
            [docker_control.DOCKER_BIN, "start", "homelab-test"],
        )

    @patch.object(docker_control.subprocess, "run")
    def test_logs_are_bounded(self, run):
        run.return_value.returncode = 0
        run.return_value.stdout = "recent logs"
        run.return_value.stderr = ""

        ok, result = docker_control.run_action("homelab-test", "logs")

        self.assertTrue(ok)
        self.assertEqual(result["logs"], "recent logs")
        command = run.call_args.args[0]
        self.assertEqual(command[1:4], ["logs", "--tail", str(docker_control.MAX_LOG_LINES)])


if __name__ == "__main__":
    unittest.main()
