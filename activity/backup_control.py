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
BACKUP_SCRIPT = Path("/home/chinookz/homelab/docker/imfs-db/backup.sh")
BACKUP_ROOT = Path("/home/chinookz/homelab/docker/imfs-db/backups")
BACKUP_LOCK = BACKUP_ROOT / ".server-monitor-backup.lock"
RCLONE_BIN = "/usr/bin/rclone"
RCLONE_CONFIG = "/home/chinookz/.config/rclone/rclone.conf"
RCLONE_REMOTE = "gdrive:IMFS-Backups/"
REQUEST_RE = re.compile(r"^[a-f0-9]{32}$")
BACKUP_NAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{6}$")


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
        if action not in {"backup", "archive"}:
            raise ValueError("unsupported backup action")
        if action == "archive":
            name = value.get("backup_name")
            if not isinstance(name, str) or not BACKUP_NAME_RE.fullmatch(name):
                raise ValueError("invalid backup name")
        return value
    except (OSError, ValueError, TypeError, KeyError):
        return None


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


def collect_status(status=None, message=None, request_id=None):
    local = local_status()
    cloud = cloud_status()
    storage = storage_status()
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

    current = collect_status(request_id=request_id)
    local = current["last_local"]
    cloud = current["last_cloud"]
    created_new = (
        local["status"] == "successful"
        and (local["name"] != before.get("name") or local["timestamp"] != before.get("timestamp"))
    )
    uploaded = cloud["status"] == "successful" and cloud["name"] == local["name"]
    if result.returncode == 0 and created_new and uploaded:
        return collect_status("successful", "Local backup and Google Drive upload completed", request_id)
    return collect_status("failed", "Backup did not complete locally and in Google Drive", request_id)


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
