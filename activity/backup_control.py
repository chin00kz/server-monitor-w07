#!/usr/bin/env python3
"""Restricted host-side status collector and launcher for IMFS backups."""

import json
import os
import re
import subprocess
import tempfile
import time
import zipfile
from datetime import datetime
from pathlib import Path

try:
    import fcntl
except ImportError:
    fcntl = None


DATA_ROOT = Path(os.environ.get(
    "SERVER_MONITOR_DATA",
    "/home/chinookz/homelab/docker/server-monitor/data",
))
CONTROL_ROOT = DATA_ROOT / "backup-control"
REQUEST_FILE = CONTROL_ROOT / "request.json"
STATUS_FILE = CONTROL_ROOT / "status.json"
LOCK_FILE = CONTROL_ROOT / ".controller.lock"
ARCHIVE_ROOT = CONTROL_ROOT / "archives"
ARCHIVE_STATUS_ROOT = CONTROL_ROOT / "archive-status"
ORACLE_STATUS_FILE = CONTROL_ROOT / "oracle-sync-status.json"
ORACLE_LOG_FILE = CONTROL_ROOT / "oracle-sync.log"
ORACLE_DEPLOY_LOCK = Path.home() / "imfs-oracle" / ".deploy.lock"
ORACLE_EXPORT_SCRIPT = Path.home() / "imfs-oracle" / "export-integration.sh"
ORACLE_SOURCE_DIR = Path.home() / "imfs-oracle" / "deploy"
ORACLE_HOST = "chinookz@144.24.152.14"
ORACLE_SSH_KEY = str(Path.home() / ".ssh" / "imfs_oracle_deploy")
ORACLE_DBS = ("imfs_central", "imfs_tenant_apex")
ORACLE_LOG_MAX_BYTES = 200_000
BACKUP_SCRIPT = Path("/home/chinookz/homelab/docker/imfs-db/backup.sh")
BACKUP_ROOT = Path("/home/chinookz/homelab/docker/imfs-db/backups")
BACKUP_LOCK = BACKUP_ROOT / ".server-monitor-backup.lock"
RCLONE_BIN = "/usr/bin/rclone"
RCLONE_CONFIG = "/home/chinookz/.config/rclone/rclone.conf"
RCLONE_REMOTE = "gdrive:IMFS-Backups/"
REQUEST_RE = re.compile(r"^[a-f0-9]{32}$")
BACKUP_NAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{6}$")
HASH_RE = re.compile(r"^[a-f0-9]{64}$")
ORACLE_ACTIONS = {"oracle_prepare", "oracle_apply"}
ALLOWED_ACTIONS = {"backup", "archive"} | ORACLE_ACTIONS


def atomic_write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(payload, output, separators=(",", ":"))
            output.write("\n")
        os.chmod(temporary, 0o660)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_request():
    try:
        value = json.loads(REQUEST_FILE.read_text())
        if not isinstance(value, dict):
            raise ValueError("invalid request")
        request_id = value.get("request_id")
        if not isinstance(request_id, str) or not REQUEST_RE.fullmatch(request_id):
            raise ValueError("invalid request ID")
        action = value.get("action")
        if action not in ALLOWED_ACTIONS:
            raise ValueError("unsupported backup action")
        if action == "archive":
            name = value.get("backup_name")
            if not isinstance(name, str) or not BACKUP_NAME_RE.fullmatch(name):
                raise ValueError("invalid backup name")
        return value
    except (OSError, ValueError, TypeError, KeyError):
        return None


def oracle_live_enabled():
    """Live Dell→Oracle commands stay off unless explicitly enabled on the host."""
    return os.environ.get("ORACLE_SYNC_LIVE", "0") == "1"


def current_boot_id():
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
        if value:
            return value
    except OSError:
        pass
    return "unknown"


def append_oracle_log(message):
    CONTROL_ROOT.mkdir(parents=True, exist_ok=True)
    line = message if message.endswith("\n") else f"{message}\n"
    try:
        with ORACLE_LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(line)
        size = ORACLE_LOG_FILE.stat().st_size
        if size > ORACLE_LOG_MAX_BYTES:
            data = ORACLE_LOG_FILE.read_bytes()[-ORACLE_LOG_MAX_BYTES:]
            ORACLE_LOG_FILE.write_bytes(data)
    except OSError:
        pass


def read_oracle_status():
    try:
        value = json.loads(ORACLE_STATUS_FILE.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, TypeError):
        return None


def write_oracle_status(**fields):
    current = read_oracle_status() or {}
    payload = {
        "status": fields.get("status", current.get("status", "unavailable")),
        "timestamp": int(time.time()),
        "message": fields.get("message", current.get("message", "")),
        "request_id": fields.get("request_id", current.get("request_id")),
        "boot_id": fields.get("boot_id", current.get("boot_id")),
        "dump_hashes": fields.get("dump_hashes", current.get("dump_hashes") or {}),
        "started_at": fields.get("started_at", current.get("started_at")),
        "finished_at": fields.get("finished_at", current.get("finished_at")),
        "duration_seconds": fields.get(
            "duration_seconds", current.get("duration_seconds")
        ),
        "exit_status": fields.get("exit_status", current.get("exit_status")),
        "apply_consumed": fields.get(
            "apply_consumed", current.get("apply_consumed", False)
        ),
        "apply_started": fields.get(
            "apply_started", current.get("apply_started", False)
        ),
    }
    atomic_write(ORACLE_STATUS_FILE, payload)
    return payload


def invalidate_stale_oracle_preparation():
    """Drop awaiting confirmation after Dell reboot; do not authorize apply."""
    status = read_oracle_status()
    if not status or status.get("status") != "awaiting_confirmation":
        return False
    stored = status.get("boot_id")
    if not isinstance(stored, str) or not stored or stored == current_boot_id():
        return False
    append_oracle_log(
        "Preparation invalidated: Dell boot ID changed; confirmation cleared."
    )
    write_oracle_status(
        status="cancelled",
        message=(
            "Preparation invalidated after Dell reboot; "
            "run Sync prepare again before apply"
        ),
        apply_consumed=True,
        finished_at=int(time.time()),
        dump_hashes={},
    )
    return True


def oracle_ssh(remote_command, timeout=120):
    return subprocess.run(
        [
            "/usr/bin/ssh",
            "-i", ORACLE_SSH_KEY,
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=20",
            ORACLE_HOST,
            remote_command,
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def parse_sha256sum_output(output, expected_names):
    hashes = {}
    for line in (output or "").splitlines():
        parts = line.strip().split()
        if len(parts) < 2:
            continue
        digest, path = parts[0], parts[-1]
        name = Path(path).name
        if name.endswith(".dump"):
            name = name[:-5]
        if name in expected_names and HASH_RE.fullmatch(digest):
            hashes[name] = digest
    if set(hashes) != set(expected_names):
        raise ValueError("incomplete checksum set")
    return hashes


def verify_staged_dump_hashes(expected):
    """Confirm Oracle staged dumps still match the preparation hashes."""
    result = oracle_ssh(
        "sudo -n sha256sum "
        "/var/lib/imfs-deploy/incoming/imfs_central.dump "
        "/var/lib/imfs-deploy/incoming/imfs_tenant_apex.dump",
        timeout=60,
    )
    append_oracle_log(result.stdout or "")
    if result.stderr:
        append_oracle_log(result.stderr)
    if result.returncode != 0:
        raise RuntimeError("Could not read staged dump checksums on Oracle")
    actual = parse_sha256sum_output(result.stdout, ORACLE_DBS)
    for name in ORACLE_DBS:
        if actual.get(name) != expected.get(name):
            raise RuntimeError(
                f"Staged dump checksum mismatch for {name}; preparation is no longer valid"
            )
    return actual


def run_oracle_prepare_commands():
    """
    Non-destructive Dell→Oracle preparation (live deploy.sh lines before confirm).
    Does not replace live Oracle databases.
    """
    if not oracle_live_enabled():
        raise RuntimeError(
            "Live Oracle sync is disabled (set ORACLE_SYNC_LIVE=1 only after approved enablement)"
        )

    ORACLE_DEPLOY_LOCK.parent.mkdir(parents=True, exist_ok=True)
    with ORACLE_DEPLOY_LOCK.open("w") as deploy_lock:
        if fcntl is None:
            raise RuntimeError("POSIX locking is required for Oracle prepare")
        try:
            fcntl.flock(deploy_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("Another IMFS deployment lock is held") from error

        append_oracle_log("Checking Oracle health...")
        health = oracle_ssh(
            "sudo -n /usr/local/sbin/imfs-db-restore health", timeout=60
        )
        append_oracle_log(health.stdout or "")
        if health.returncode != 0 or (health.stdout or "").strip() != "healthy":
            raise RuntimeError("Oracle health check failed")

        append_oracle_log("Exporting Dell integration databases...")
        export = subprocess.run(
            [str(ORACLE_EXPORT_SCRIPT)],
            capture_output=True,
            text=True,
            timeout=3600,
            check=False,
        )
        append_oracle_log(export.stdout or "")
        if export.stderr:
            append_oracle_log(export.stderr)
        if export.returncode != 0 or "EXPORT_SUCCESS" not in (export.stdout or ""):
            raise RuntimeError("Dell export failed")

        append_oracle_log("Transferring dumps to Oracle...")
        dumps = [ORACLE_SOURCE_DIR / f"{name}.dump" for name in ORACLE_DBS]
        for path in dumps:
            if not path.is_file() or path.is_symlink():
                raise RuntimeError(f"Missing export dump: {path.name}")
        transfer = subprocess.run(
            [
                "/usr/bin/scp",
                "-i", ORACLE_SSH_KEY,
                "-o", "BatchMode=yes",
                *[str(path) for path in dumps],
                f"{ORACLE_HOST}:imfs-oracle/incoming/",
            ],
            capture_output=True,
            text=True,
            timeout=3600,
            check=False,
        )
        if transfer.stdout:
            append_oracle_log(transfer.stdout)
        if transfer.stderr:
            append_oracle_log(transfer.stderr)
        if transfer.returncode != 0:
            raise RuntimeError("SCP transfer to Oracle failed")

        append_oracle_log("Verifying transfer checksums...")
        local_hashes = {}
        for name in ORACLE_DBS:
            local = subprocess.run(
                ["/usr/bin/sha256sum", str(ORACLE_SOURCE_DIR / f"{name}.dump")],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            if local.returncode != 0:
                raise RuntimeError(f"Local checksum failed for {name}")
            local_hashes[name] = local.stdout.split()[0]
            remote = oracle_ssh(
                f"sha256sum imfs-oracle/incoming/{name}.dump", timeout=120
            )
            append_oracle_log(remote.stdout or "")
            if remote.returncode != 0:
                raise RuntimeError(f"Remote checksum failed for {name}")
            remote_hash = remote.stdout.split()[0]
            if local_hashes[name] != remote_hash:
                raise RuntimeError(f"Transfer checksum mismatch: {name}")

        append_oracle_log("Staging dumps on Oracle...")
        stage = oracle_ssh(
            "sudo -n /usr/local/sbin/imfs-db-restore stage-dumps", timeout=300
        )
        append_oracle_log(stage.stdout or "")
        if stage.stderr:
            append_oracle_log(stage.stderr)
        if stage.returncode != 0 or "STAGE_DUMPS_SUCCESS" not in (stage.stdout or ""):
            raise RuntimeError("Oracle stage-dumps failed")

        staged = parse_sha256sum_output(stage.stdout, ORACLE_DBS)
        for name in ORACLE_DBS:
            if staged[name] != local_hashes[name]:
                raise RuntimeError(f"Staged checksum mismatch: {name}")
        append_oracle_log("Oracle staging completed; awaiting confirmation.")
        return local_hashes


def run_oracle_apply_commands():
    """Destructive apply once. Never auto-retry after failure."""
    if not oracle_live_enabled():
        raise RuntimeError(
            "Live Oracle sync is disabled (set ORACLE_SYNC_LIVE=1 only after approved enablement)"
        )

    ORACLE_DEPLOY_LOCK.parent.mkdir(parents=True, exist_ok=True)
    with ORACLE_DEPLOY_LOCK.open("w") as deploy_lock:
        if fcntl is None:
            raise RuntimeError("POSIX locking is required for Oracle apply")
        try:
            fcntl.flock(deploy_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("Another IMFS deployment lock is held") from error

        append_oracle_log("Starting protected Oracle restore (imfs-db-restore apply)...")
        append_oracle_log(
            "Note: live apply drops existing imfs_next_* databases before recreate."
        )
        result = oracle_ssh(
            "sudo -n /usr/local/sbin/imfs-db-restore apply CONFIRM-REPLACE-IMFS",
            timeout=7200,
        )
        append_oracle_log(result.stdout or "")
        if result.stderr:
            append_oracle_log(result.stderr)
        if result.returncode != 0 or "DEPLOYMENT_SUCCESS" not in (result.stdout or ""):
            raise RuntimeError(
                f"Oracle apply failed with exit code {result.returncode}; "
                "do not retry blindly after an uncertain cutover"
            )
        append_oracle_log("===== DEPLOYMENT COMPLETE =====")
        return result.returncode


def run_oracle_prepare(request):
    request_id = request["request_id"]
    started = int(time.time())
    try:
        ORACLE_LOG_FILE.write_text("", encoding="utf-8")
    except OSError:
        pass
    append_oracle_log(f"oracle_prepare start request_id={request_id}")
    write_oracle_status(
        status="preparing",
        message="Preparing Dell export and Oracle staging",
        request_id=request_id,
        boot_id=current_boot_id(),
        started_at=started,
        finished_at=None,
        duration_seconds=None,
        exit_status=None,
        dump_hashes={},
        apply_consumed=False,
        apply_started=False,
    )
    try:
        hashes = run_oracle_prepare_commands()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        finished = int(time.time())
        append_oracle_log(f"oracle_prepare failed: {error}")
        return write_oracle_status(
            status="failed",
            message=str(error)[:300],
            request_id=request_id,
            finished_at=finished,
            duration_seconds=finished - started,
            exit_status=1,
            apply_consumed=True,
            dump_hashes={},
        )

    finished = int(time.time())
    return write_oracle_status(
        status="awaiting_confirmation",
        message=(
            "Preparation succeeded. Type REPLACE-ORACLE-IMFS to replace "
            "live Oracle databases imfs_central and imfs_tenant_apex."
        ),
        request_id=request_id,
        boot_id=current_boot_id(),
        dump_hashes=hashes,
        started_at=started,
        finished_at=finished,
        duration_seconds=finished - started,
        exit_status=0,
        apply_consumed=False,
        apply_started=False,
    )


def run_oracle_apply(request):
    request_id = request["request_id"]
    status = read_oracle_status() or {}
    # API may already have moved awaiting → applying and set apply_consumed to
    # prevent confirmation replay before this oneshot runs.
    queued_by_api = (
        status.get("status") == "applying"
        and status.get("apply_consumed") is True
        and status.get("request_id") == request_id
        and not status.get("apply_started")
    )
    awaiting = (
        status.get("status") == "awaiting_confirmation"
        and status.get("request_id") == request_id
        and not status.get("apply_consumed")
        and not status.get("apply_started")
    )
    if not queued_by_api and not awaiting:
        append_oracle_log(
            "oracle_apply rejected: no matching preparation awaiting confirmation"
        )
        return write_oracle_status(
            status="failed",
            message="No valid preparation is awaiting confirmation for this request",
            request_id=request_id,
            apply_consumed=True,
            apply_started=True,
            finished_at=int(time.time()),
            exit_status=1,
        )

    if status.get("boot_id") != current_boot_id():
        append_oracle_log("oracle_apply rejected: Dell boot ID no longer matches")
        return write_oracle_status(
            status="cancelled",
            message="Preparation invalidated after Dell reboot",
            request_id=request_id,
            apply_consumed=True,
            apply_started=True,
            finished_at=int(time.time()),
            dump_hashes={},
            exit_status=1,
        )

    expected = status.get("dump_hashes") or {}
    if set(expected) != set(ORACLE_DBS) or not all(
        isinstance(expected.get(name), str) and HASH_RE.fullmatch(expected[name])
        for name in ORACLE_DBS
    ):
        return write_oracle_status(
            status="failed",
            message="Preparation hashes are missing; run prepare again",
            request_id=request_id,
            apply_consumed=True,
            apply_started=True,
            finished_at=int(time.time()),
            exit_status=1,
        )

    started = int(status.get("started_at") or time.time())
    # Mark started before any destructive work so a second oneshot cannot re-enter.
    write_oracle_status(
        status="applying",
        message="Applying staged dumps to Oracle (destructive cutover)",
        request_id=request_id,
        started_at=started,
        finished_at=None,
        duration_seconds=None,
        exit_status=None,
        apply_consumed=True,
        apply_started=True,
        dump_hashes=expected,
    )
    append_oracle_log(f"oracle_apply start request_id={request_id}")

    try:
        verify_staged_dump_hashes(expected)
        exit_status = run_oracle_apply_commands()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        finished = int(time.time())
        append_oracle_log(f"oracle_apply failed: {error}")
        return write_oracle_status(
            status="failed",
            message=str(error)[:300],
            request_id=request_id,
            finished_at=finished,
            duration_seconds=finished - started,
            exit_status=1,
            apply_consumed=True,
        )

    finished = int(time.time())
    return write_oracle_status(
        status="successful",
        message="Oracle IMFS databases replaced successfully",
        request_id=request_id,
        finished_at=finished,
        duration_seconds=finished - started,
        exit_status=exit_status,
        apply_consumed=True,
    )


def local_status():
    try:
        folders = [item for item in BACKUP_ROOT.iterdir() if item.is_dir()]
        if not folders:
            return {"status": "unavailable", "timestamp": None, "name": None}
        latest = max(folders, key=lambda item: item.stat().st_mtime)
        complete = (latest / "SUCCESS").is_file() and (latest / "manifest.txt").is_file()
        return {
            "status": "successful" if complete else "failed",
            "timestamp": int(latest.stat().st_mtime),
            "name": latest.name[:128],
        }
    except OSError:
        return {"status": "unavailable", "timestamp": None, "name": None}


def list_local_backups():
    backups = []
    try:
        root = BACKUP_ROOT.resolve(strict=True)
        for item in BACKUP_ROOT.iterdir():
            if (
                not BACKUP_NAME_RE.fullmatch(item.name)
                or item.is_symlink()
                or not item.is_dir()
                or item.resolve(strict=True).parent != root
            ):
                continue
            try:
                created = int(datetime.strptime(
                    item.name, "%Y-%m-%d_%H%M%S"
                ).astimezone().timestamp())
            except ValueError:
                continue
            backups.append({"name": item.name, "timestamp": created})
    except OSError:
        return []
    return sorted(backups, key=lambda item: item["name"], reverse=True)[:100]


def parse_rclone_time(value):
    if not isinstance(value, str):
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def cloud_status():
    try:
        result = subprocess.run(
            [RCLONE_BIN, "--config", RCLONE_CONFIG, "lsjson", RCLONE_REMOTE,
             "--recursive", "--files-only",
             "--include", "*/SUCCESS", "--include", "*/manifest.txt"],
            capture_output=True, text=True, timeout=45, check=True,
        )
        entries = json.loads(result.stdout)
        folders = {}
        for item in entries if isinstance(entries, list) else []:
            path = item.get("Path") if isinstance(item, dict) else None
            timestamp = parse_rclone_time(item.get("ModTime")) if isinstance(item, dict) else None
            if not isinstance(path, str) or timestamp is None:
                continue
            filename = Path(path).name
            if filename not in {"SUCCESS", "manifest.txt"}:
                continue
            folder = str(Path(path).parent)
            folders.setdefault(folder, {})[filename] = timestamp
        candidates = [
            (max(files.values()), Path(folder).name[:128], files)
            for folder, files in folders.items()
        ]
        if not candidates:
            return {"status": "unavailable", "timestamp": None, "name": None}
        timestamp, name, files = max(candidates, key=lambda item: item[0])
        complete = {"SUCCESS", "manifest.txt"}.issubset(files)
        return {
            "status": "successful" if complete else "failed",
            "timestamp": timestamp,
            "name": name,
        }
    except (OSError, ValueError, subprocess.SubprocessError):
        return {"status": "unavailable", "timestamp": None, "name": None}


def storage_status():
    try:
        result = subprocess.run(
            [RCLONE_BIN, "--config", RCLONE_CONFIG, "about", "gdrive:", "--json"],
            capture_output=True, text=True, timeout=30, check=True,
        )
        value = json.loads(result.stdout)
        total = value.get("total")
        used = value.get("used")
        free = value.get("free")
        if not all(type(item) is int and item >= 0 for item in (total, used, free)):
            raise ValueError("invalid storage values")
        return {"status": "ok", "total": total, "used": used, "free": free}
    except (OSError, ValueError, subprocess.SubprocessError):
        return {"status": "unavailable", "total": None, "used": None, "free": None}


def collect_status(
    status=None, message=None, request_id=None, *,
    local=None, cloud=None, storage=None,
):
    local = local if local is not None else local_status()
    cloud = cloud if cloud is not None else cloud_status()
    storage = storage if storage is not None else storage_status()
    if status is None:
        if local["status"] == "failed" or cloud["status"] == "failed":
            status = "failed"
            message = "The latest local or cloud backup is incomplete"
        elif local["status"] == cloud["status"] == "successful":
            status = "successful"
            message = "Local and cloud backups are available"
        else:
            status = "unavailable"
            message = "Complete local and cloud backup status is unavailable"
    payload = {
        "status": status,
        "timestamp": int(time.time()),
        "message": message,
        "last_local": local,
        "last_cloud": cloud,
        "storage": storage,
        "backups": list_local_backups(),
    }
    if request_id:
        payload["request_id"] = request_id
    atomic_write(STATUS_FILE, payload)
    return payload


def backup_process_running():
    try:
        result = subprocess.run(
            ["/usr/bin/pgrep", "-f", "--", re.escape(str(BACKUP_SCRIPT))],
            capture_output=True, text=True, timeout=5, check=False,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return True


def verify_cloud_backup(backup_name, attempts=4, delay_seconds=2):
    """Retry bounded cloud listing until the exact new backup is complete."""
    latest = {"status": "unavailable", "timestamp": None, "name": None}
    for attempt in range(attempts):
        latest = cloud_status()
        if latest["status"] == "successful" and latest["name"] == backup_name:
            return latest
        if attempt + 1 < attempts:
            time.sleep(delay_seconds)
    return latest


def run_backup(request):
    request_id = request["request_id"]
    if backup_process_running():
        return collect_status("failed", "A scheduled or manual backup is already running", request_id)

    before = local_status()
    atomic_write(STATUS_FILE, {
        "status": "running",
        "timestamp": int(time.time()),
        "message": "Backup is running",
        "request_id": request_id,
        "last_local": before,
        "last_cloud": {"status": "unavailable", "timestamp": None, "name": None},
        "storage": {"status": "unavailable", "total": None, "used": None, "free": None},
        "backups": list_local_backups(),
    })
    try:
        result = subprocess.run(
            ["/usr/bin/flock", "--nonblock", str(BACKUP_LOCK), str(BACKUP_SCRIPT)],
            capture_output=True, text=True,
            timeout=7200, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return collect_status("failed", "Backup script could not be completed", request_id)

    local = local_status()
    created_new = (
        local["status"] == "successful"
        and (local["name"] != before.get("name") or local["timestamp"] != before.get("timestamp"))
    )

    if result.returncode != 0:
        cloud = cloud_status()
        return collect_status(
            "failed", f"Backup script failed with exit code {result.returncode}",
            request_id, local=local, cloud=cloud,
        )

    if not created_new:
        cloud = cloud_status()
        return collect_status(
            "failed", "Backup script completed but no new successful local backup was found",
            request_id, local=local, cloud=cloud,
        )

    cloud = verify_cloud_backup(local["name"])
    if cloud["status"] == "successful" and cloud["name"] == local["name"]:
        return collect_status(
            "successful", "Local backup and Google Drive upload completed",
            request_id, local=local, cloud=cloud,
        )

    if cloud["status"] == "successful":
        message = "Google Drive verification found a different backup folder"
    elif cloud["status"] == "failed":
        message = "Google Drive verification found an incomplete backup upload"
    else:
        message = "Google Drive verification did not find the new backup"
    return collect_status(
        "failed", message, request_id, local=local, cloud=cloud,
    )


def archive_status_path(request_id):
    return ARCHIVE_STATUS_ROOT / f"{request_id}.json"


def write_archive_status(request_id, status, message, backup_name):
    atomic_write(archive_status_path(request_id), {
        "request_id": request_id,
        "status": status,
        "message": message,
        "backup_name": backup_name,
        "timestamp": int(time.time()),
    })


def safe_backup_folder(name):
    if not isinstance(name, str) or not BACKUP_NAME_RE.fullmatch(name):
        raise ValueError("invalid backup name")
    root = BACKUP_ROOT.resolve(strict=True)
    source = BACKUP_ROOT / name
    if source.is_symlink() or not source.is_dir():
        raise ValueError("backup folder unavailable")
    resolved = source.resolve(strict=True)
    if resolved.parent != root:
        raise ValueError("backup path is outside the backup directory")
    return resolved


def create_archive(request):
    request_id = request["request_id"]
    backup_name = request["backup_name"]
    write_archive_status(request_id, "running", "Creating ZIP archive", backup_name)
    temporary = None
    try:
        source = safe_backup_folder(backup_name)
        ARCHIVE_ROOT.mkdir(parents=True, exist_ok=True)
        os.chmod(ARCHIVE_ROOT, 0o770)
        final = ARCHIVE_ROOT / f"{request_id}.zip"
        temporary = ARCHIVE_ROOT / f".{request_id}.tmp"

        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
            for current_root, directories, files in os.walk(source, followlinks=False):
                current = Path(current_root)
                for name in [*directories, *files]:
                    item = current / name
                    if item.is_symlink():
                        raise ValueError("backup contains a symbolic link")
                    resolved = item.resolve(strict=True)
                    try:
                        relative = resolved.relative_to(source)
                    except ValueError as error:
                        raise ValueError("backup entry escapes its folder") from error
                    archive_name = Path(backup_name) / relative
                    if item.is_dir():
                        archive.writestr(str(archive_name).replace("\\", "/") + "/", b"")
                    elif item.is_file():
                        archive.write(item, str(archive_name).replace("\\", "/"))

        os.chmod(temporary, 0o660)
        os.replace(temporary, final)
        write_archive_status(request_id, "completed", "ZIP archive is ready", backup_name)
    except (OSError, ValueError, zipfile.BadZipFile):
        write_archive_status(request_id, "failed", "Backup archive could not be created", backup_name)
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


def cleanup_archives():
    cutoff = time.time() - 3600
    for root in (ARCHIVE_ROOT, ARCHIVE_STATUS_ROOT):
        try:
            for item in root.iterdir():
                if item.is_file() and not item.is_symlink() and item.stat().st_mtime < cutoff:
                    item.unlink()
        except OSError:
            pass


def main():
    if fcntl is None:
        raise RuntimeError("backup controller requires POSIX file locking")
    CONTROL_ROOT.mkdir(parents=True, exist_ok=True)
    with LOCK_FILE.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0

        # Reboot invalidation runs without holding a human-confirmation wait.
        invalidate_stale_oracle_preparation()

        if not REQUEST_FILE.exists():
            cleanup_archives()
            collect_status()
            return 0

        request = read_request()
        try:
            if request is None:
                collect_status("failed", "Invalid backup request")
            elif request["action"] == "archive":
                create_archive(request)
            elif request["action"] == "oracle_prepare":
                run_oracle_prepare(request)
            elif request["action"] == "oracle_apply":
                run_oracle_apply(request)
            else:
                run_backup(request)
        finally:
            try:
                REQUEST_FILE.unlink()
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
