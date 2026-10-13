#!/usr/bin/env python3
"""Restricted host-side status collector and launcher for IMFS backups."""

import json
import os
import re
import subprocess
import tempfile
import time
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
BACKUP_SCRIPT = Path("/home/chinookz/homelab/docker/imfs-db/backup.sh")
BACKUP_ROOT = Path("/home/chinookz/homelab/docker/imfs-db/backups")
BACKUP_LOCK = BACKUP_ROOT / ".server-monitor-backup.lock"
RCLONE_BIN = "/usr/bin/rclone"
RCLONE_CONFIG = "/home/chinookz/.config/rclone/rclone.conf"
RCLONE_REMOTE = "gdrive:IMFS-Backups/"
REQUEST_RE = re.compile(r"^[a-f0-9]{32}$")


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
        if value.get("action") != "backup":
            raise ValueError("unsupported backup action")
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
            collect_status()
            return 0

        request = read_request()
        try:
            if request is None:
                collect_status("failed", "Invalid backup request")
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
