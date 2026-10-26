import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch


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

                request.write_text(json.dumps({
                    "request_id": "a" * 32,
                    "action": "archive",
                    "backup_name": "2026-10-13_224152",
                }))
                self.assertEqual(
                    backup_control.read_request()["backup_name"],
                    "2026-10-13_224152",
                )
                request.write_text(json.dumps({
                    "request_id": "a" * 32,
                    "action": "archive",
                    "backup_name": "../../etc",
                }))
                self.assertIsNone(backup_control.read_request())

                request.write_text(json.dumps({
                    "request_id": "a" * 32, "action": "oracle_prepare",
                }))
                self.assertEqual(
                    backup_control.read_request()["action"], "oracle_prepare",
                )
                request.write_text(json.dumps({
                    "request_id": "a" * 32, "action": "oracle_apply",
                }))
                self.assertEqual(
                    backup_control.read_request()["action"], "oracle_apply",
                )

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
    @patch.object(backup_control, "verify_cloud_backup")
    @patch.object(backup_control, "local_status")
    @patch.object(backup_control, "backup_process_running", return_value=False)
    @patch.object(backup_control.subprocess, "run")
    @patch.object(backup_control, "atomic_write")
    def test_manual_backup_invokes_only_fixed_script_through_lock(
        self, atomic_write, run, process_running, local_status,
        verify_cloud_backup, collect_status,
    ):
        before = {"status": "successful", "timestamp": 10, "name": "old"}
        local = {"status": "successful", "timestamp": 20, "name": "new"}
        cloud = {"status": "successful", "timestamp": 21, "name": "new"}
        local_status.side_effect = [before, local]
        verify_cloud_backup.return_value = cloud
        run.return_value = Mock(returncode=0)
        collect_status.return_value = {"status": "successful"}

        result = backup_control.run_backup({"request_id": "b" * 32})

        self.assertEqual(result["status"], "successful")
        self.assertEqual(run.call_args.args[0], [
            "/usr/bin/flock", "--nonblock", str(backup_control.BACKUP_LOCK),
            str(backup_control.BACKUP_SCRIPT),
        ])
        process_running.assert_called_once_with()
        verify_cloud_backup.assert_called_once_with("new")
        collect_status.assert_called_once_with(
            "successful", "Local backup and Google Drive upload completed", "b" * 32,
            local=local, cloud=cloud,
        )

    @patch.object(backup_control.time, "sleep")
    @patch.object(backup_control, "cloud_status")
    def test_cloud_verification_retries_then_accepts_exact_backup(self, cloud_status, sleep):
        cloud_status.side_effect = [
            {"status": "unavailable", "timestamp": None, "name": None},
            {"status": "successful", "timestamp": 20, "name": "new"},
        ]

        result = backup_control.verify_cloud_backup("new")

        self.assertEqual(result["status"], "successful")
        self.assertEqual(cloud_status.call_count, 2)
        sleep.assert_called_once_with(2)

    @patch.object(backup_control, "collect_status")
    @patch.object(backup_control, "cloud_status")
    @patch.object(backup_control, "local_status")
    @patch.object(backup_control, "backup_process_running", return_value=False)
    @patch.object(backup_control.subprocess, "run")
    @patch.object(backup_control, "atomic_write")
    def test_script_failure_is_never_reported_successfully(
        self, atomic_write, run, process_running, local_status,
        cloud_status, collect_status,
    ):
        before = {"status": "successful", "timestamp": 10, "name": "old"}
        local = {"status": "successful", "timestamp": 20, "name": "new"}
        cloud = {"status": "successful", "timestamp": 21, "name": "new"}
        local_status.side_effect = [before, local]
        cloud_status.return_value = cloud
        run.return_value = Mock(returncode=7)
        collect_status.return_value = {"status": "failed"}

        result = backup_control.run_backup({"request_id": "d" * 32})

        self.assertEqual(result["status"], "failed")
        collect_status.assert_called_once_with(
            "failed", "Backup script failed with exit code 7", "d" * 32,
            local=local, cloud=cloud,
        )

    @patch.object(backup_control, "collect_status")
    @patch.object(backup_control, "cloud_status")
    @patch.object(backup_control, "local_status")
    @patch.object(backup_control, "backup_process_running", return_value=False)
    @patch.object(backup_control.subprocess, "run")
    @patch.object(backup_control, "atomic_write")
    def test_missing_new_local_backup_has_specific_failure(
        self, atomic_write, run, process_running, local_status,
        cloud_status, collect_status,
    ):
        unchanged = {"status": "successful", "timestamp": 10, "name": "old"}
        cloud = {"status": "successful", "timestamp": 10, "name": "old"}
        local_status.side_effect = [unchanged, unchanged]
        cloud_status.return_value = cloud
        run.return_value = Mock(returncode=0)
        collect_status.return_value = {"status": "failed"}

        backup_control.run_backup({"request_id": "e" * 32})

        collect_status.assert_called_once_with(
            "failed", "Backup script completed but no new successful local backup was found",
            "e" * 32, local=unchanged, cloud=cloud,
        )

    @patch.object(backup_control, "collect_status")
    @patch.object(backup_control, "verify_cloud_backup")
    @patch.object(backup_control, "local_status")
    @patch.object(backup_control, "backup_process_running", return_value=False)
    @patch.object(backup_control.subprocess, "run")
    @patch.object(backup_control, "atomic_write")
    def test_cloud_verification_failure_keeps_same_component_snapshot(
        self, atomic_write, run, process_running, local_status,
        verify_cloud_backup, collect_status,
    ):
        before = {"status": "successful", "timestamp": 10, "name": "old"}
        local = {"status": "successful", "timestamp": 20, "name": "new"}
        cloud = {"status": "unavailable", "timestamp": None, "name": None}
        local_status.side_effect = [before, local]
        verify_cloud_backup.return_value = cloud
        run.return_value = Mock(returncode=0)
        collect_status.return_value = {
            "status": "failed", "last_local": local, "last_cloud": cloud,
        }

        result = backup_control.run_backup({"request_id": "f" * 32})

        self.assertEqual(result["last_local"], local)
        self.assertEqual(result["last_cloud"], cloud)
        collect_status.assert_called_once_with(
            "failed", "Google Drive verification did not find the new backup",
            "f" * 32, local=local, cloud=cloud,
        )

    @patch.object(backup_control, "collect_status")
    @patch.object(backup_control, "backup_process_running", return_value=True)
    def test_running_scheduled_backup_rejects_manual_run(self, process_running, collect_status):
        collect_status.return_value = {"status": "failed"}

        backup_control.run_backup({"request_id": "c" * 32})

        collect_status.assert_called_once_with(
            "failed", "A scheduled or manual backup is already running", "c" * 32
        )

    def test_archive_contains_complete_folder_and_nested_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "backups"
            source = root / "2026-10-13_224152"
            nested = source / "database"
            nested.mkdir(parents=True)
            (source / "SUCCESS").write_text("ok")
            (source / "manifest.txt").write_text("manifest")
            (nested / "dump.sql").write_text("select 1;")
            control = Path(directory) / "control"
            archives = control / "archives"
            statuses = control / "archive-status"
            request = {
                "request_id": "d" * 32,
                "action": "archive",
                "backup_name": source.name,
            }

            with (
                patch.object(backup_control, "BACKUP_ROOT", root),
                patch.object(backup_control, "ARCHIVE_ROOT", archives),
                patch.object(backup_control, "ARCHIVE_STATUS_ROOT", statuses),
            ):
                backup_control.create_archive(request)

            archive_path = archives / f'{request["request_id"]}.zip'
            with zipfile.ZipFile(archive_path) as archive:
                self.assertEqual(set(archive.namelist()), {
                    "2026-10-13_224152/SUCCESS",
                    "2026-10-13_224152/manifest.txt",
                    "2026-10-13_224152/database/",
                    "2026-10-13_224152/database/dump.sql",
                })
            status = json.loads((statuses / f'{request["request_id"]}.json').read_text())
            self.assertEqual(status["status"], "completed")

    def test_safe_backup_folder_rejects_traversal(self):
        with self.assertRaises(ValueError):
            backup_control.safe_backup_folder("../../etc")

    def test_oracle_health_output_ok_markers_and_failures(self):
        ok = backup_control.oracle_health_output_ok
        multiline = (
            "healthy\n"
            "health=healthy\n"
            "container=imfs-pg-integration\n"
            "HEALTH_OK\n"
        )
        self.assertTrue(ok(0, multiline))
        self.assertTrue(ok(0, "healthy\n"))
        self.assertTrue(ok(0, "health=healthy\n"))
        self.assertFalse(ok(1, multiline))
        self.assertFalse(ok(0, ""))
        self.assertFalse(ok(0, "unhealthy\n"))
        self.assertFalse(ok(0, "health=unhealthy\n"))
        # Success markers must not override an explicit unhealthy result.
        self.assertFalse(ok(0, "HEALTH_OK\nhealth=unhealthy\n"))
        self.assertFalse(ok(0, "healthy\nunhealthy\n"))
        # Whole-line match only: do not treat "unhealthy" as containing "healthy".
        self.assertFalse(ok(0, "unhealthy\n"))

    def test_oracle_prepare_reaches_awaiting_confirmation_with_mocked_commands(self):
        hashes = {"imfs_central": "a" * 64, "imfs_tenant_apex": "b" * 64}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            status = root / "oracle-sync-status.json"
            log = root / "oracle-sync.log"
            with (
                patch.object(backup_control, "ORACLE_STATUS_FILE", status),
                patch.object(backup_control, "ORACLE_LOG_FILE", log),
                patch.object(backup_control, "CONTROL_ROOT", root),
                patch.object(backup_control, "current_boot_id", return_value="boot-1"),
                patch.object(
                    backup_control, "run_oracle_prepare_commands", return_value=hashes,
                ),
            ):
                result = backup_control.run_oracle_prepare({"request_id": "c" * 32})

            self.assertEqual(result["status"], "awaiting_confirmation")
            self.assertEqual(result["dump_hashes"], hashes)
            self.assertFalse(result["apply_consumed"])
            self.assertIn("oracle_prepare start", log.read_text())
            self.assertIn("REPLACE-ORACLE-IMFS", result["message"])

    def test_oracle_prepare_disable_flag_fails_without_subprocess(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            status = root / "oracle-sync-status.json"
            log = root / "oracle-sync.log"
            with (
                patch.object(backup_control, "ORACLE_STATUS_FILE", status),
                patch.object(backup_control, "ORACLE_LOG_FILE", log),
                patch.object(backup_control, "CONTROL_ROOT", root),
                patch.object(backup_control, "current_boot_id", return_value="boot-1"),
                patch.dict(
                    backup_control.os.environ, {"ORACLE_SYNC_DISABLE": "1"}, clear=False,
                ),
                patch.object(backup_control.subprocess, "run") as run,
            ):
                result = backup_control.run_oracle_prepare({"request_id": "d" * 32})

            self.assertEqual(result["status"], "failed")
            self.assertIn("disabled", result["message"].lower())
            run.assert_not_called()

    def test_oracle_prepare_missing_export_script_fails_clearly(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            status = root / "oracle-sync-status.json"
            log = root / "oracle-sync.log"
            missing_export = root / "missing-export.sh"
            with (
                patch.object(backup_control, "ORACLE_STATUS_FILE", status),
                patch.object(backup_control, "ORACLE_LOG_FILE", log),
                patch.object(backup_control, "CONTROL_ROOT", root),
                patch.object(backup_control, "ORACLE_EXPORT_SCRIPT", missing_export),
                patch.object(backup_control, "ORACLE_SSH_KEY", str(root / "key")),
                patch.object(backup_control, "current_boot_id", return_value="boot-1"),
                patch.dict(
                    backup_control.os.environ, {"ORACLE_SYNC_DISABLE": "0"}, clear=False,
                ),
                patch.object(backup_control.subprocess, "run") as run,
            ):
                (root / "key").write_text("dummy")
                result = backup_control.run_oracle_prepare({"request_id": "9" * 32})

            self.assertEqual(result["status"], "failed")
            self.assertIn("missing dell export script", result["message"].lower())
            run.assert_not_called()

    def test_oracle_apply_verifies_hashes_and_rejects_second_start(self):
        hashes = {"imfs_central": "a" * 64, "imfs_tenant_apex": "b" * 64}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            status = root / "oracle-sync-status.json"
            log = root / "oracle-sync.log"
            status.write_text(json.dumps({
                "status": "applying",
                "timestamp": 2_000_000_000,
                "message": "queued",
                "request_id": "e" * 32,
                "boot_id": "boot-1",
                "dump_hashes": hashes,
                "apply_consumed": True,
                "apply_started": False,
                "started_at": 2_000_000_000,
            }))
            with (
                patch.object(backup_control, "ORACLE_STATUS_FILE", status),
                patch.object(backup_control, "ORACLE_LOG_FILE", log),
                patch.object(backup_control, "CONTROL_ROOT", root),
                patch.object(backup_control, "current_boot_id", return_value="boot-1"),
                patch.object(backup_control, "verify_staged_dump_hashes", return_value=hashes),
                patch.object(backup_control, "run_oracle_apply_commands", return_value=0),
            ):
                first = backup_control.run_oracle_apply({"request_id": "e" * 32})
                second = backup_control.run_oracle_apply({"request_id": "e" * 32})

            self.assertEqual(first["status"], "successful")
            self.assertTrue(first["apply_started"])
            self.assertEqual(second["status"], "failed")

    def test_oracle_apply_rejects_boot_id_change_and_does_not_call_apply(self):
        hashes = {"imfs_central": "a" * 64, "imfs_tenant_apex": "b" * 64}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            status = root / "oracle-sync-status.json"
            log = root / "oracle-sync.log"
            status.write_text(json.dumps({
                "status": "awaiting_confirmation",
                "timestamp": 2_000_000_000,
                "message": "ready",
                "request_id": "f" * 32,
                "boot_id": "boot-old",
                "dump_hashes": hashes,
                "apply_consumed": False,
                "apply_started": False,
            }))
            with (
                patch.object(backup_control, "ORACLE_STATUS_FILE", status),
                patch.object(backup_control, "ORACLE_LOG_FILE", log),
                patch.object(backup_control, "CONTROL_ROOT", root),
                patch.object(backup_control, "current_boot_id", return_value="boot-new"),
                patch.object(backup_control, "verify_staged_dump_hashes") as verify,
                patch.object(backup_control, "run_oracle_apply_commands") as apply_cmd,
            ):
                result = backup_control.run_oracle_apply({"request_id": "f" * 32})

            self.assertEqual(result["status"], "cancelled")
            verify.assert_not_called()
            apply_cmd.assert_not_called()

    def test_invalidate_stale_oracle_preparation_on_reboot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            status = root / "oracle-sync-status.json"
            log = root / "oracle-sync.log"
            status.write_text(json.dumps({
                "status": "awaiting_confirmation",
                "timestamp": 2_000_000_000,
                "message": "ready",
                "request_id": "1" * 32,
                "boot_id": "before-reboot",
                "dump_hashes": {"imfs_central": "a" * 64, "imfs_tenant_apex": "b" * 64},
                "apply_consumed": False,
            }))
            with (
                patch.object(backup_control, "ORACLE_STATUS_FILE", status),
                patch.object(backup_control, "ORACLE_LOG_FILE", log),
                patch.object(backup_control, "CONTROL_ROOT", root),
                patch.object(backup_control, "current_boot_id", return_value="after-reboot"),
            ):
                changed = backup_control.invalidate_stale_oracle_preparation()
                payload = json.loads(status.read_text())

            self.assertTrue(changed)
            self.assertEqual(payload["status"], "cancelled")
            self.assertEqual(payload["dump_hashes"], {})

    def test_failed_apply_is_not_retried_automatically(self):
        hashes = {"imfs_central": "a" * 64, "imfs_tenant_apex": "b" * 64}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            status = root / "oracle-sync-status.json"
            log = root / "oracle-sync.log"
            status.write_text(json.dumps({
                "status": "applying",
                "timestamp": 2_000_000_000,
                "message": "queued",
                "request_id": "2" * 32,
                "boot_id": "boot-1",
                "dump_hashes": hashes,
                "apply_consumed": True,
                "apply_started": False,
                "started_at": 2_000_000_000,
            }))
            with (
                patch.object(backup_control, "ORACLE_STATUS_FILE", status),
                patch.object(backup_control, "ORACLE_LOG_FILE", log),
                patch.object(backup_control, "CONTROL_ROOT", root),
                patch.object(backup_control, "current_boot_id", return_value="boot-1"),
                patch.object(backup_control, "verify_staged_dump_hashes", return_value=hashes),
                patch.object(
                    backup_control,
                    "run_oracle_apply_commands",
                    side_effect=RuntimeError("cutover failed; do not retry"),
                ),
            ):
                failed = backup_control.run_oracle_apply({"request_id": "2" * 32})
                retry = backup_control.run_oracle_apply({"request_id": "2" * 32})

            self.assertEqual(failed["status"], "failed")
            self.assertIn("cutover failed", failed["message"])
            self.assertEqual(retry["status"], "failed")
            self.assertIn("no valid preparation", retry["message"].lower())


if __name__ == "__main__":
    unittest.main()
