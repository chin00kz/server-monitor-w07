import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch


ACTIVITY = Path(__file__).resolve().parents[1] / "activity"
sys.path.insert(0, str(ACTIVITY))

import backup_control  # noqa: E402


class BackupControlTests(unittest.TestCase):
    def test_request_accepts_only_fixed_backup_action(self):
        with tempfile.TemporaryDirectory() as directory:
            request = Path(directory) / "request.json"
            with patch.object(backup_control, "REQUEST_FILE", request):
                request.write_text(json.dumps({
                    "request_id": "a" * 32, "action": "backup",
                }))
                self.assertEqual(backup_control.read_request()["action"], "backup")
                request.write_text(json.dumps({
                    "request_id": "a" * 32, "action": "shell",
                }))
                self.assertIsNone(backup_control.read_request())

    @patch.object(backup_control.subprocess, "run")
    def test_storage_uses_fixed_rclone_about_command(self, run):
        run.return_value = Mock(stdout=json.dumps({
            "total": 1000, "used": 250, "free": 750,
        }))

        result = backup_control.storage_status()

        self.assertEqual(result["status"], "ok")
        self.assertEqual(
            run.call_args.args[0],
            [backup_control.RCLONE_BIN, "--config", backup_control.RCLONE_CONFIG,
             "about", "gdrive:", "--json"],
        )

    @patch.object(backup_control.subprocess, "run")
    def test_latest_incomplete_cloud_backup_is_failed(self, run):
        run.return_value = Mock(stdout=json.dumps([
            {"Path": "2026-10-13/SUCCESS", "ModTime": "2026-10-13T02:10:00Z"},
            {"Path": "2026-10-13/manifest.txt", "ModTime": "2026-10-13T02:09:00Z"},
            {"Path": "incomplete/SUCCESS", "ModTime": "2026-10-14T02:10:00Z"},
        ]))

        result = backup_control.cloud_status()

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["name"], "incomplete")
        self.assertEqual(run.call_args.args[0][:5], [
            backup_control.RCLONE_BIN, "--config", backup_control.RCLONE_CONFIG,
            "lsjson", backup_control.RCLONE_REMOTE,
        ])

    @patch.object(backup_control, "collect_status")
    @patch.object(backup_control, "local_status")
    @patch.object(backup_control, "backup_process_running", return_value=False)
    @patch.object(backup_control.subprocess, "run")
    @patch.object(backup_control, "atomic_write")
    def test_manual_backup_invokes_only_fixed_script_through_lock(
        self, atomic_write, run, process_running, local_status, collect_status,
    ):
        before = {"status": "successful", "timestamp": 10, "name": "old"}
        current = {
            "last_local": {"status": "successful", "timestamp": 20, "name": "new"},
            "last_cloud": {"status": "successful", "timestamp": 21, "name": "new"},
        }
        local_status.return_value = before
        run.return_value = Mock(returncode=0)
        collect_status.side_effect = [current, {"status": "successful"}]

        result = backup_control.run_backup({"request_id": "b" * 32})

        self.assertEqual(result["status"], "successful")
        self.assertEqual(run.call_args.args[0], [
            "/usr/bin/flock", "--nonblock", str(backup_control.BACKUP_LOCK),
            str(backup_control.BACKUP_SCRIPT),
        ])
        process_running.assert_called_once_with()
        self.assertEqual(collect_status.call_args_list[-1], call(
            "successful", "Local backup and Google Drive upload completed", "b" * 32
        ))

    @patch.object(backup_control, "collect_status")
    @patch.object(backup_control, "backup_process_running", return_value=True)
    def test_running_scheduled_backup_rejects_manual_run(self, process_running, collect_status):
        collect_status.return_value = {"status": "failed"}

        backup_control.run_backup({"request_id": "c" * 32})

        collect_status.assert_called_once_with(
            "failed", "A scheduled or manual backup is already running", "c" * 32
        )


if __name__ == "__main__":
    unittest.main()
