#!/usr/bin/env python3
"""Narrow host-side Docker controller for dashboard requests."""

import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

try:
    import fcntl
except ImportError:
    fcntl = None

DATA_ROOT = Path(os.environ.get("SERVER_MONITOR_DATA", "/home/chinookz/homelab/docker/server-monitor/data"))
CONTROL_ROOT = DATA_ROOT / "docker-control"
REQUEST_FILE = CONTROL_ROOT / "request.json"
STATUS_FILE = CONTROL_ROOT / "status.json"
AUDIT_FILE = DATA_ROOT / "activity" / "docker-control.log"
LOCK_FILE = CONTROL_ROOT / ".controller.lock"
DOCKER_BIN = os.environ.get("DOCKER_BIN", "/usr/bin/docker")
ALLOWED_ACTIONS = {"start", "stop", "restart", "logs"}
EXCLUDED_CONTAINERS = {"server-monitor"}
LOG_ONLY_CONTAINERS = {"server-monitor"}
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
REQUEST_RE = re.compile(r"^[a-f0-9]{32}$")
MAX_LOG_LINES = 200
MAX_LOG_BYTES = 32 * 1024


def configured_containers():
    raw = os.environ.get(
        "DOCKER_CONTROL_CONTAINERS",
        "imfs-pg-m1,imfs-pg-m2,imfs-pg-m3,imfs-pg-m4,imfs-pg-integration,"
        "homelab-test,homelab-ollama",
    )
    return {
        name.strip()
        for name in raw.split(",")
        if NAME_RE.fullmatch(name.strip()) and name.strip() not in EXCLUDED_CONTAINERS
    }


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


def read_json(path):
    try:
        with path.open(encoding="utf-8") as source:
            value = json.load(source)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, TypeError):
        return None


def redact(value):
    text = value[:MAX_LOG_BYTES]
    configured = [item for item in os.environ.get("DOCKER_LOG_REDACTIONS", "").split(",") if item]
    for secret in configured:
        text = text.replace(secret[:128], "[REDACTED]")
    text = re.sub(r"(?i)(password|token|secret|authorization)=([^\s&]+)", r"\1=[REDACTED]", text)
    text = re.sub(r"(?<![\w])/(?:home|etc|var|opt|srv|root|tmp)/[^\s]+", "[PATH]", text)
    return text


def inspect_container(container):
    command = [
        DOCKER_BIN,
        "inspect",
        "--type=container",
        "--format={{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}",
        container,
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=10, check=True)
        state, health = result.stdout.strip().split("|", 1)
        return {"state": state, "health": health}
    except (OSError, ValueError, subprocess.SubprocessError):
        return {"state": "unknown", "health": "unknown"}


def audit(request, outcome, detail=""):
    entry = {
        "timestamp": int(time.time()),
        "request_id": request.get("request_id"),
        "container": request.get("container"),
        "action": request.get("action"),
        "outcome": outcome,
    }
    if detail:
        entry["detail"] = redact(detail)[:1000]
    try:
        AUDIT_FILE.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT_FILE.open("a", encoding="utf-8") as output:
            output.write(json.dumps(entry, separators=(",", ":")) + "\n")
        os.chmod(AUDIT_FILE, 0o660)
    except OSError:
        return False
    return True


def validate_request(request):
    if not isinstance(request, dict):
        raise ValueError("invalid request")
    request_id = request.get("request_id")
    container = request.get("container")
    action = request.get("action")
    if not isinstance(request_id, str) or not REQUEST_RE.fullmatch(request_id):
        raise ValueError("invalid request ID")
    if not isinstance(container, str) or not NAME_RE.fullmatch(container):
        raise ValueError("invalid container name")
    if container in LOG_ONLY_CONTAINERS and action != "logs":
        raise PermissionError("container is authorized for logs only")
    if container not in configured_containers() and container not in LOG_ONLY_CONTAINERS:
        raise PermissionError("container is not authorized")
    if action not in ALLOWED_ACTIONS:
        raise ValueError("unsupported Docker action")
    return request_id, container, action


def run_action(container, action):
    if action == "logs":
        command = [DOCKER_BIN, "logs", "--tail", str(MAX_LOG_LINES), "--timestamps", container]
        result = subprocess.run(command, capture_output=True, text=True, timeout=15, check=False)
        output = redact(result.stdout + result.stderr)
        if result.returncode != 0:
            return False, {"message": "Docker logs failed", "logs": output}
        return True, {"message": "Logs loaded", "logs": output}

    result = subprocess.run(
        [DOCKER_BIN, action, container],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        return False, {"message": f"Docker {action} failed", "detail": redact(result.stderr)[:1000]}
    return True, {"message": f"Docker {action} completed"}


def process_request(request):
    request_id, container, action = validate_request(request)
    running = {
        "request_id": request_id,
        "container": container,
        "action": action,
        "status": "running",
        "timestamp": int(time.time()),
    }
    atomic_write(STATUS_FILE, running)
    ok, result = run_action(container, action)
    if action != "logs":
        result.update(inspect_container(container))
    response = {
        **running,
        "status": "completed" if ok else "failed",
        **result,
        "finished_at": int(time.time()),
    }
    atomic_write(STATUS_FILE, response)
    audit(request, response["status"], response.get("detail", ""))


def main():
    if not REQUEST_FILE.exists():
        return 0
    request = read_json(REQUEST_FILE)
    try:
        if request is None:
            raise ValueError("invalid request payload")
        process_request(request)
    except Exception as error:
        request = request or {}
        failure = {
            "request_id": request.get("request_id"),
            "container": request.get("container"),
            "action": request.get("action"),
            "status": "failed",
            "message": str(error),
            "finished_at": int(time.time()),
        }
        atomic_write(STATUS_FILE, failure)
        audit(request, "failed", str(error))
    finally:
        try:
            REQUEST_FILE.unlink()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    if fcntl is None:
        raise SystemExit("Docker controller requires POSIX file locking")
    CONTROL_ROOT.mkdir(parents=True, exist_ok=True)
    with LOCK_FILE.open("w") as lock:
        deadline = time.monotonic() + 45
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise SystemExit("controller lock timeout")
                time.sleep(0.25)
        raise SystemExit(main())
