"""server-monitor step 4: collector + SQLite history + login + API."""
import json
import os
import re
import secrets
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from datetime import datetime
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

from activity_snapshot import load_activity_snapshot
from backup_status import load_backup_status
from docker_uptime import calculate_uptime_seconds
from network_health import ALL_INTERFACES, network_status_ok
from oracle_sync_status import load_oracle_sync_status
from service_expectations import container_roles
from snapshot_sanitize import sanitize_postgres_database
import store
from host import PROC, SYS, Collector

HASH_FILE = Path("/secrets/admin.hash")
WEB = Path(__file__).parent / "web"
SESSION_TTL, MAX_FAILS, FAIL_WINDOW = 7 * 86400, 5, 900
SAMPLE_SECONDS = int(os.environ.get("SAMPLE_SECONDS", "15"))
KEY_RE = re.compile(r"^[a-z0-9_.]{1,64}$")
ACTIVITY_SNAPSHOT = Path("/data/activity/latest.json")

latest = {"ts": None, "m": {}, "info": {}}
ph = PasswordHasher()
sessions: dict[str, float] = {}
fails: dict[str, list[float]] = {}


collector = Collector()
collector_lock = threading.Lock()
last_sample_time = 0.0


def sample_metrics(slow: bool = True):
    """Sample host metrics, persist to SQLite, and update latest state thread-safely."""
    global last_sample_time
    with collector_lock:
        now = time.time()
        # Avoid dividing by near-zero dt if triggered faster than 1 second
        if now - last_sample_time < 1.0 and latest["ts"] is not None:
            return latest["ts"], latest["m"], latest["info"]
        conn = store.connect()
        try:
            ts, metrics, info = collector.sample(slow=slow)
            store.insert(conn, ts, metrics)
            latest.update(ts=ts, m={**latest["m"], **metrics}, info=info)
            last_sample_time = now
            return ts, metrics, info
        finally:
            conn.close()


def collector_loop():
    n, last_rollup = 0, 0.0
    while True:
        started = time.time()
        try:
            sample_metrics(slow=(n % 2 == 0))
            if started - last_rollup > 3600:
                conn = store.connect()
                try:
                    store.rollup(conn)
                finally:
                    conn.close()
                last_rollup = started
        except Exception as e:  # never stop collecting
            print("collector error:", repr(e), flush=True)
        n += 1
        time.sleep(max(1.0, SAMPLE_SECONDS - (time.time() - started)))


@asynccontextmanager
async def lifespan(_app):
    store.init()
    threading.Thread(target=collector_loop, daemon=True).start()
    yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    return resp


# ---------- auth ----------
def logged_in(request: Request) -> bool:
    exp = sessions.get(request.cookies.get("sm_session", ""))
    return bool(exp and exp > time.time())


def require_login(request: Request):
    if not logged_in(request):
        raise HTTPException(401, "login required")


def require_management(request: Request):
    require_login(request)
    origin = request.headers.get("origin") or request.headers.get("referer")
    host = request.headers.get("host")
    if not origin or not host or urlsplit(origin).netloc != host:
        raise HTTPException(403, "same-origin request required")


class Login(BaseModel):
    password: str


@app.post("/api/login")
def login(body: Login, request: Request, response: Response):
    if not HASH_FILE.exists():
        raise HTTPException(503, "no password set")
    ip = request.client.host if request.client else "?"
    now = time.time()
    recent = [t for t in fails.get(ip, []) if now - t < FAIL_WINDOW]
    if len(recent) >= MAX_FAILS:
        raise HTTPException(429, "too many attempts")
    try:
        ph.verify(HASH_FILE.read_text().strip(), body.password)
    except (VerifyMismatchError, InvalidHashError, OSError):
        fails[ip] = recent + [now]
        time.sleep(1)
        raise HTTPException(401, "wrong password")
    fails.pop(ip, None)
    for k in [k for k, v in sessions.items() if v < now]:
        del sessions[k]
    token = secrets.token_urlsafe(32)
    sessions[token] = now + SESSION_TTL
    response.set_cookie("sm_session", token, max_age=SESSION_TTL,
                        httponly=True, samesite="strict", path="/")
    return {"ok": True}


@app.post("/api/logout")
def logout(request: Request, response: Response):
    sessions.pop(request.cookies.get("sm_session", ""), None)
    response.delete_cookie("sm_session", path="/")
    return {"ok": True}


# ---------- pages ----------
@app.get("/favicon.svg")
@app.get("/favicon.ico")
def favicon():
    """Browser tab icon; .ico alias serves the same SVG for older agents."""
    return FileResponse(WEB / "favicon.svg", media_type="image/svg+xml")


@app.get("/login")
def login_page():
    return FileResponse(WEB / "login.html")


@app.get("/")
def index(request: Request):
    if not logged_in(request):
        return RedirectResponse("/login", status_code=303)
    return FileResponse(WEB / "index.html")


@app.get("/staging")
def staging(request: Request):
    if not logged_in(request):
        return RedirectResponse("/login", status_code=303)
    return FileResponse(WEB / "index.staging.html")


@app.get("/api/health")
def health():  # unauthenticated on purpose (Docker healthcheck): booleans only
    return {"status": "ok",
            "proc_mounted": (PROC / "uptime").exists(),
            "sys_mounted": (SYS / "class").exists()}


# ---------- data ----------
@app.get("/api/now", dependencies=[Depends(require_login)])
def now():
    return latest


HOST_TRIGGER_FILE = Path("/data/activity/refresh.request")
HOST_STATUS_FILE = Path("/data/activity/refresh.status")
last_host_request_time = 0.0
host_request_lock = threading.Lock()
DOCKER_CONTROL_ROOT = Path("/data/docker-control")
DOCKER_CONTROL_REQUEST = DOCKER_CONTROL_ROOT / "request.json"
DOCKER_CONTROL_STATUS = DOCKER_CONTROL_ROOT / "status.json"
DOCKER_CONTROL_ACTIONS = {"start", "stop", "restart", "logs"}
DOCKER_CONTROL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
DOCKER_CONTROL_EXCLUDED = {"server-monitor"}
DOCKER_CONTROL_LOG_ONLY = {"server-monitor"}
# Host path unit should consume request.json quickly; abandon abandoned files.
DOCKER_CONTROL_STALE_SECONDS = 90
last_docker_action_time: dict[str, float] = {}
docker_control_lock = threading.Lock()
BACKUP_CONTROL_ROOT = Path("/data/backup-control")
BACKUP_CONTROL_REQUEST = BACKUP_CONTROL_ROOT / "request.json"
BACKUP_CONTROL_STATUS = BACKUP_CONTROL_ROOT / "status.json"
BACKUP_ARCHIVE_ROOT = BACKUP_CONTROL_ROOT / "archives"
BACKUP_ARCHIVE_STATUS_ROOT = BACKUP_CONTROL_ROOT / "archive-status"
ORACLE_SYNC_STATUS = BACKUP_CONTROL_ROOT / "oracle-sync-status.json"
ORACLE_SYNC_LOG = BACKUP_CONTROL_ROOT / "oracle-sync.log"
ORACLE_CONFIRM_PHRASE = "REPLACE-ORACLE-IMFS"
BACKUP_NAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{6}$")
BACKUP_REQUEST_RE = re.compile(r"^[a-f0-9]{32}$")
backup_control_lock = threading.Lock()


class DockerControlRequest(BaseModel):
    container: str
    action: str


class OracleSyncConfirm(BaseModel):
    confirmation: str
    request_id: str


def oracle_sync_live_enabled() -> bool:
    return os.environ.get("ORACLE_SYNC_LIVE", "0") == "1"


def read_oracle_sync_raw() -> dict | None:
    try:
        value = json.loads(ORACLE_SYNC_STATUS.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, TypeError):
        return None


def oracle_operation_busy(raw: dict | None = None) -> bool:
    """True while prepare/apply holds the host controller; not while awaiting humans."""
    status = (raw or read_oracle_sync_raw() or {}).get("status")
    return status in {"preparing", "applying"}


def current_oracle_sync(now: int | None = None) -> dict:
    return load_oracle_sync_status(
        ORACLE_SYNC_STATUS,
        ORACLE_SYNC_LOG,
        int(time.time() if now is None else now),
        live_enabled=oracle_sync_live_enabled(),
    )


def configured_docker_containers():
    raw = os.environ.get(
        "DOCKER_CONTROL_CONTAINERS",
        "imfs-pg-m1,imfs-pg-m2,imfs-pg-m3,imfs-pg-m4,imfs-pg-integration,"
        "homelab-test,homelab-ollama",
    )
    return {
        name.strip()
        for name in raw.split(",")
        if DOCKER_CONTROL_NAME_RE.fullmatch(name.strip())
        and name.strip() not in DOCKER_CONTROL_EXCLUDED
    }


def write_control_json(path: Path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, separators=(",", ":")) + "\n")
    try:
        os.chmod(temporary, 0o660)
    except OSError:
        pass
    os.replace(temporary, path)


def read_control_status():
    try:
        value = json.loads(DOCKER_CONTROL_STATUS.read_text())
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, TypeError):
        return None


def _docker_request_age_seconds(now: float) -> float | None:
    """Age of the pending request.json, or None if it is missing."""
    if not DOCKER_CONTROL_REQUEST.exists():
        return None
    try:
        payload = json.loads(DOCKER_CONTROL_REQUEST.read_text())
        created = payload.get("created_at") if isinstance(payload, dict) else None
        if isinstance(created, (int, float)):
            return max(0.0, now - float(created))
    except (OSError, ValueError, TypeError):
        pass
    try:
        return max(0.0, now - DOCKER_CONTROL_REQUEST.stat().st_mtime)
    except OSError:
        return DOCKER_CONTROL_STALE_SECONDS + 1.0


def clear_stale_docker_request(now: float) -> bool:
    """Drop abandoned request.json so a dead host controller cannot brick controls."""
    age = _docker_request_age_seconds(now)
    if age is None or age < DOCKER_CONTROL_STALE_SECONDS:
        return False
    payload = None
    try:
        value = json.loads(DOCKER_CONTROL_REQUEST.read_text())
        if isinstance(value, dict):
            payload = value
    except (OSError, ValueError, TypeError):
        payload = None
    try:
        DOCKER_CONTROL_REQUEST.unlink(missing_ok=True)
    except OSError:
        return False
    write_control_json(DOCKER_CONTROL_STATUS, {
        "request_id": (payload or {}).get("request_id"),
        "container": (payload or {}).get("container"),
        "action": (payload or {}).get("action"),
        "status": "failed",
        "message": "Previous Docker request was abandoned (host controller did not consume it)",
        "finished_at": int(now),
        "timestamp": int(now),
    })
    return True


@app.get("/api/backups", dependencies=[Depends(require_login)])
def backup_status():
    """Return validated status produced by the restricted host controller."""
    return load_backup_status(BACKUP_CONTROL_STATUS, int(time.time()))


@app.post("/api/backups/run", dependencies=[Depends(require_management)])
def run_backup():
    """Queue the fixed host backup script; no browser-supplied command is accepted."""
    now = int(time.time())
    with backup_control_lock:
        if BACKUP_CONTROL_REQUEST.exists():
            raise HTTPException(409, "a backup is already queued")
        if oracle_operation_busy():
            raise HTTPException(409, "an Oracle sync operation is already running")
        current = load_backup_status(BACKUP_CONTROL_STATUS, now)
        if current["status"] == "running":
            raise HTTPException(409, "a backup is already running")

        request = {
            "request_id": secrets.token_hex(16),
            "action": "backup",
            "created_at": now,
        }
        running = {
            "status": "running",
            "timestamp": now,
            "message": "Backup request queued",
            "request_id": request["request_id"],
            "last_local": current["last_local"],
            "last_cloud": current["last_cloud"],
            "storage": current["storage"],
        }
        write_control_json(BACKUP_CONTROL_STATUS, running)
        write_control_json(BACKUP_CONTROL_REQUEST, request)
    return running


@app.post(
    "/api/backups/{backup_name}/archive",
    dependencies=[Depends(require_management)],
)
def request_backup_archive(backup_name: str):
    """Queue creation of one validated backup ZIP on the host."""
    if not BACKUP_NAME_RE.fullmatch(backup_name):
        raise HTTPException(400, "invalid backup name")

    now = int(time.time())
    with backup_control_lock:
        if BACKUP_CONTROL_REQUEST.exists():
            raise HTTPException(409, "a backup operation is already queued")
        if oracle_operation_busy():
            raise HTTPException(409, "an Oracle sync operation is already running")
        current = load_backup_status(BACKUP_CONTROL_STATUS, now)
        available = {item["name"] for item in current.get("backups", [])}
        if backup_name not in available:
            raise HTTPException(404, "backup folder is unavailable")

        request_id = secrets.token_hex(16)
        request = {
            "request_id": request_id,
            "action": "archive",
            "backup_name": backup_name,
            "created_at": now,
        }
        write_control_json(BACKUP_ARCHIVE_STATUS_ROOT / f"{request_id}.json", {
            "request_id": request_id,
            "status": "queued",
            "message": "ZIP archive queued",
            "backup_name": backup_name,
            "timestamp": now,
        })
        write_control_json(BACKUP_CONTROL_REQUEST, request)
    return {"request_id": request_id, "status": "queued"}


@app.get("/api/backups/oracle-sync", dependencies=[Depends(require_login)])
def oracle_sync_status():
    """Return persisted Oracle sync status and bounded log text."""
    return current_oracle_sync()


@app.post(
    "/api/backups/oracle-sync/prepare",
    dependencies=[Depends(require_management)],
)
def oracle_sync_prepare():
    """Queue fixed non-destructive Dell→Oracle preparation (no live DB replace)."""
    now = int(time.time())
    with backup_control_lock:
        if BACKUP_CONTROL_REQUEST.exists():
            raise HTTPException(409, "a backup or sync operation is already queued")
        backup = load_backup_status(BACKUP_CONTROL_STATUS, now)
        if backup["status"] == "running":
            raise HTTPException(409, "a backup is already running")
        raw = read_oracle_sync_raw() or {}
        if oracle_operation_busy(raw):
            raise HTTPException(409, "an Oracle sync operation is already running")
        if raw.get("status") == "awaiting_confirmation" and not raw.get("apply_consumed"):
            raise HTTPException(
                409,
                "a preparation is already awaiting confirmation",
            )

        request_id = secrets.token_hex(16)
        request = {
            "request_id": request_id,
            "action": "oracle_prepare",
            "created_at": now,
        }
        preparing = {
            "status": "preparing",
            "timestamp": now,
            "message": "Oracle sync prepare queued",
            "request_id": request_id,
            "boot_id": None,
            "dump_hashes": {},
            "started_at": now,
            "finished_at": None,
            "duration_seconds": None,
            "exit_status": None,
            "apply_consumed": False,
        }
        write_control_json(ORACLE_SYNC_STATUS, preparing)
        write_control_json(BACKUP_CONTROL_REQUEST, request)
    return current_oracle_sync(now)


@app.post(
    "/api/backups/oracle-sync/confirm",
    dependencies=[Depends(require_management)],
)
def oracle_sync_confirm(body: OracleSyncConfirm):
    """
    Queue fixed destructive apply after exact phrase confirmation.
    The confirmation phrase is never written to status or request files.
    """
    confirmation = body.confirmation if isinstance(body.confirmation, str) else ""
    request_id = body.request_id if isinstance(body.request_id, str) else ""
    if not secrets.compare_digest(confirmation, ORACLE_CONFIRM_PHRASE):
        raise HTTPException(400, "confirmation phrase is incorrect")
    if not BACKUP_REQUEST_RE.fullmatch(request_id):
        raise HTTPException(400, "invalid preparation request")

    now = int(time.time())
    with backup_control_lock:
        if BACKUP_CONTROL_REQUEST.exists():
            raise HTTPException(409, "a backup or sync operation is already queued")
        backup = load_backup_status(BACKUP_CONTROL_STATUS, now)
        if backup["status"] == "running":
            raise HTTPException(409, "a backup is already running")

        raw = read_oracle_sync_raw() or {}
        if (
            raw.get("status") != "awaiting_confirmation"
            or raw.get("request_id") != request_id
            or raw.get("apply_consumed")
        ):
            raise HTTPException(
                409,
                "no valid preparation is awaiting confirmation for this request",
            )

        request = {
            "request_id": request_id,
            "action": "oracle_apply",
            "created_at": now,
        }
        applying = {
            "status": "applying",
            "timestamp": now,
            "message": "Oracle sync apply queued",
            "request_id": request_id,
            "boot_id": raw.get("boot_id"),
            "dump_hashes": raw.get("dump_hashes") or {},
            "started_at": now,
            "finished_at": None,
            "duration_seconds": None,
            "exit_status": None,
            # Consume confirmation before the host runs apply so refresh/replay
            # cannot re-queue; apply_started stays false until the controller begins.
            "apply_consumed": True,
            "apply_started": False,
        }
        write_control_json(ORACLE_SYNC_STATUS, applying)
        write_control_json(BACKUP_CONTROL_REQUEST, request)
    return current_oracle_sync(now)


def remove_download(path: Path, status_path: Path):
    for target in (path, status_path):
        try:
            target.unlink()
        except OSError:
            pass


@app.get(
    "/api/backups/archive/{request_id}",
    dependencies=[Depends(require_login)],
)
def download_backup_archive(request_id: str):
    """Return a completed temporary archive and remove it after transfer."""
    if not BACKUP_REQUEST_RE.fullmatch(request_id):
        raise HTTPException(400, "invalid archive request")
    status_path = BACKUP_ARCHIVE_STATUS_ROOT / f"{request_id}.json"
    try:
        status = json.loads(status_path.read_text())
        if not isinstance(status, dict) or status.get("request_id") != request_id:
            raise ValueError("invalid archive status")
        state = status.get("status")
        backup_name = status.get("backup_name")
        timestamp = status.get("timestamp")
        if (
            state not in {"queued", "running", "completed", "failed"}
            or not isinstance(backup_name, str)
            or not BACKUP_NAME_RE.fullmatch(backup_name)
            or type(timestamp) is not int
            or int(time.time()) - timestamp > 3600
        ):
            raise ValueError("invalid archive status")
    except (OSError, ValueError, TypeError, KeyError):
        raise HTTPException(404, "archive request is unavailable")

    if state in {"queued", "running"}:
        return JSONResponse(status_code=202, content={"status": state})
    if state == "failed":
        raise HTTPException(500, "backup archive could not be created")

    archive = BACKUP_ARCHIVE_ROOT / f"{request_id}.zip"
    if archive.is_symlink() or not archive.is_file():
        raise HTTPException(404, "backup archive is unavailable")
    return FileResponse(
        archive,
        media_type="application/zip",
        filename=f"{backup_name}.zip",
        background=BackgroundTask(remove_download, archive, status_path),
    )


@app.post("/api/refresh", dependencies=[Depends(require_login)])
def refresh_now():
    """Trigger an immediate sample of live hardware/OS metrics and return fresh snapshot."""
    try:
        sample_metrics(slow=True)
    except Exception as e:
        raise HTTPException(500, f"Refresh failed: {e}")
    return latest


@app.post("/api/refresh/host", dependencies=[Depends(require_login)])
def refresh_host(timeout: float = 8.0):
    """Request on-demand collection via systemd path trigger and await completion."""
    global last_host_request_time
    wait_time = max(1.0, min(float(timeout), 45.0))

    with host_request_lock:
        now = time.time()
        # Rate limit new host trigger creation to once per 5 seconds
        if (
            now - last_host_request_time < 5.0
            or HOST_TRIGGER_FILE.exists()
        ):
            return {
                "status": "rate_limited",
                "message": "Host refresh is already queued or running",
            }

        req_id = secrets.token_hex(8)
        last_host_request_time = now
        HOST_TRIGGER_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp_trigger = HOST_TRIGGER_FILE.with_suffix(".tmp")
        tmp_trigger.write_text(json.dumps({"request_id": req_id, "timestamp": now}))
        try:
            os.chmod(tmp_trigger, 0o664)
        except OSError:
            pass
        os.replace(tmp_trigger, HOST_TRIGGER_FILE)

    # Wait up to wait_time seconds for host runner to complete
    deadline = time.time() + wait_time
    while time.time() < deadline:
        time.sleep(0.3)
        if HOST_STATUS_FILE.exists():
            try:
                st = json.loads(HOST_STATUS_FILE.read_text())
                if st.get("request_id") == req_id and st.get("status") == "completed":
                    return {
                        "status": "completed",
                        "request_id": req_id,
                        "timestamp": st.get("timestamp"),
                        "duration_seconds": st.get("duration_seconds"),
                    }
            except (OSError, ValueError):
                pass

    return {
        "status": "pending",
        "request_id": req_id,
        "message": "Host collection initiated or in progress",
    }


@app.post("/api/docker/control", dependencies=[Depends(require_management)])
def docker_control(body: DockerControlRequest):
    """Submit one allowlisted Docker action to the host-side controller."""
    container = body.container.strip()
    action = body.action.strip().lower()
    if not DOCKER_CONTROL_NAME_RE.fullmatch(container):
        raise HTTPException(400, "invalid container name")
    if action not in DOCKER_CONTROL_ACTIONS:
        raise HTTPException(400, "unsupported Docker action")
    if container in DOCKER_CONTROL_LOG_ONLY and action != "logs":
        raise HTTPException(403, "container is authorized for logs only")
    if (container not in configured_docker_containers()
            and container not in DOCKER_CONTROL_LOG_ONLY):
        raise HTTPException(403, "container is not authorized")

    now = time.time()
    with docker_control_lock:
        clear_stale_docker_request(now)
        if DOCKER_CONTROL_REQUEST.exists():
            raise HTTPException(
                409,
                "a Docker action is already queued — wait ~90s or check host docker-control.service",
            )
        status = read_control_status()
        if status and status.get("status") in {"queued", "running"}:
            status_time = status.get("timestamp", 0)
            if isinstance(status_time, (int, float)) and now - status_time < 120:
                raise HTTPException(409, "a Docker action is already running")
        if now - last_docker_action_time.get(container, 0) < 5:
            raise HTTPException(429, "Docker action requested recently")

        request_id = secrets.token_hex(16)
        request = {
            "request_id": request_id,
            "container": container,
            "action": action,
            "created_at": int(now),
        }
        last_docker_action_time[container] = now
        write_control_json(DOCKER_CONTROL_STATUS, {
            **request,
            "status": "queued",
            "timestamp": int(now),
            "message": "Docker action queued",
        })
        write_control_json(DOCKER_CONTROL_REQUEST, request)

    deadline = time.monotonic() + 35
    while time.monotonic() < deadline:
        time.sleep(0.25)
        status = read_control_status()
        if not status or status.get("request_id") != request_id:
            continue
        if status.get("status") in {"completed", "failed"}:
            return status

    # Leave request.json for a late host controller, but mark status so the UI
    # is clear that nothing acknowledged yet. Stale cleanup unbricks retries.
    return {
        "request_id": request_id,
        "container": container,
        "action": action,
        "status": "pending",
        "message": "Docker controller did not acknowledge within the timeout — is server-docker-control.path active?",
    }



@app.get("/api/activity", dependencies=[Depends(require_login)])
def activity():
    """Return sanitized activity, rejecting stale or malformed snapshots."""
    now_ts = int(time.time())

    try:
        snapshot, timestamp, age, safe_databases = load_activity_snapshot(
            ACTIVITY_SNAPSHOT, now_ts
        )

        sessions = snapshot.get("sessions")
        if not isinstance(sessions, dict):
            raise ValueError("session data unavailable")

        safe_sessions = {}
        for name, count_key in (("ssh", "connections"), ("local", "sessions")):
            item = sessions.get(name)
            if not isinstance(item, dict) or item.get("status") != "ok":
                safe_sessions[name] = {"status": "unknown", count_key: None}
                continue

            count = item.get(count_key)
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                safe_sessions[name] = {"status": "unknown", count_key: None}
            else:
                safe_sessions[name] = {"status": "ok", count_key: count}

        # Network readings are optional; failures must not affect DB/session status.
        raw_network = snapshot.get("network")
        safe_network = {
            "status": "unknown",
            "preferred_interface": None,
            "interfaces": {},
        }

        if isinstance(raw_network, dict):
            raw_interfaces = raw_network.get("interfaces")
            if isinstance(raw_interfaces, dict):
                for name in ALL_INTERFACES:
                    item = raw_interfaces.get(name)
                    if not isinstance(item, dict):
                        continue

                    state = item.get("operstate")
                    ip = item.get("ipv4")
                    if (
                        item.get("status") == "ok"
                        and isinstance(state, str)
                        and state in ("UP", "DOWN", "UNKNOWN", "DORMANT", "LOWERLAYERDOWN")
                        and (ip is None or (
                            isinstance(ip, str)
                            and len(ip) <= 45
                            and all(c in "0123456789." for c in ip)
                        ))
                    ):
                        safe_network["interfaces"][name] = {
                            "status": "ok",
                            "operstate": state,
                            "ipv4": ip,
                        }
                    elif isinstance(item.get("status"), str):
                        # Preserve degraded interface rows when inventory is incomplete.
                        safe_network["interfaces"][name] = {
                            "status": "unknown",
                            "operstate": state if isinstance(state, str) else None,
                            "ipv4": None,
                        }

            preferred = raw_network.get("preferred_interface")
            if preferred in ALL_INTERFACES:
                safe_network["preferred_interface"] = preferred

            # Overall ok follows preferred WAN inventory only (not Tailscale).
            # Prefer collector status when present, but recompute so API and
            # collector stay consistent even with older snapshots.
            if network_status_ok(
                safe_network["preferred_interface"],
                safe_network["interfaces"],
            ):
                safe_network["status"] = "ok"

        # Actual Wi-Fi SSID, validated before exposing it to the browser.
        safe_network["wifi_ssid"] = None
        if isinstance(raw_network, dict):
            ssid = raw_network.get("wifi_ssid")
            if (
                isinstance(ssid, str)
                and 0 < len(ssid) <= 32
                and all(ord(c) >= 32 and ord(c) != 127 for c in ssid)
            ):
                safe_network["wifi_ssid"] = ssid

        # Connectivity checks have separate refresh intervals.
        # Only expose validated values; never let network errors break DB monitoring.
        safe_network["connectivity"] = {}
        safe_network["interface_connectivity"] = {}

        if isinstance(raw_network, dict):
            raw_checks = raw_network.get("interface_connectivity")
            if isinstance(raw_checks, dict):
                for iface in ("wlp2s0", "enp1s0"):
                    item = raw_checks.get(iface)
                    if not isinstance(item, dict):
                        continue
                    status = item.get("status")
                    if status not in (
                        "reachable", "unreachable", "http_error", "unknown"
                    ):
                        status = "unknown"
                    code = item.get("http_code")
                    if (
                        not isinstance(code, int)
                        or isinstance(code, bool)
                        or not 100 <= code <= 599
                    ):
                        code = None
                    ms = item.get("response_ms")
                    if (
                        not isinstance(ms, (int, float))
                        or isinstance(ms, bool)
                        or not 0 <= ms <= 30000
                    ):
                        ms = None
                    safe_network["interface_connectivity"][iface] = {
                        "status": status,
                        "http_code": code,
                        "response_ms": ms,
                    }

        if isinstance(raw_network, dict):
            raw_connectivity = raw_network.get("connectivity")

            if isinstance(raw_connectivity, dict):
                for name in ("starlink", "dialog"):
                    item = raw_connectivity.get(name)
                    if not isinstance(item, dict):
                        continue

                    status = item.get("status")
                    code = item.get("http_code")
                    response_ms = item.get("response_ms")

                    if status not in (
                        "reachable", "unreachable", "http_error", "unknown"
                    ):
                        status = "unknown"

                    if (
                        not isinstance(code, int)
                        or isinstance(code, bool)
                        or not 100 <= code <= 599
                    ):
                        code = None

                    if (
                        not isinstance(response_ms, (int, float))
                        or isinstance(response_ms, bool)
                        or not 0 <= response_ms <= 30000
                    ):
                        response_ms = None

                    safe_network["connectivity"][name] = {
                        "status": status,
                        "http_code": code,
                        "response_ms": response_ms,
                    }

            for key in ("active_checked_at", "backup_checked_at"):
                value = raw_network.get(key)
                safe_network[key] = (
                    value
                    if isinstance(value, int)
                    and not isinstance(value, bool)
                    and 0 < value <= now_ts
                    else None
                )

        return {
            "status": "ok",
            "timestamp": timestamp,
            "age_seconds": age,
            "databases": safe_databases,
            "sessions": safe_sessions,
            "network": safe_network,
        }

    except (OSError, ValueError, TypeError, KeyError):
        return {
            "status": "unknown",
            "timestamp": None,
            "age_seconds": None,
            "databases": {},
        }


@app.get("/api/series", dependencies=[Depends(require_login)])
def get_series(keys: str, hours: float = 6):
    ks = [k for k in keys.split(",") if KEY_RE.match(k)][:16]
    if not ks:
        raise HTTPException(400, "no valid keys")
    return store.series(ks, hours)


@app.get("/api/network/usage-today", dependencies=[Depends(require_login)])
def network_usage_today():
    """Estimate observed network usage from recent SQLite speed samples."""
    tz = ZoneInfo("Asia/Colombo")
    now = datetime.now(tz)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start = int(midnight.timestamp())

    interfaces = {
        "starlink": "wifi",
        "dialog": "ethernet",
        "tailscale": "tailscale",
    }

    result = {}

    try:
        conn = store.connect()
        try:
            for name, iface in interfaces.items():
                item = {}

                for direction in ("rx", "tx"):
                    key = f"net.{iface}.{direction}"
                    rows = conn.execute(
                        "SELECT ts, v FROM s "
                        "WHERE key=? AND ts>=? AND ts<=? ORDER BY ts",
                        (key, start, int(now.timestamp())),
                    ).fetchall()

                    total = 0.0
                    covered = 0
                    previous = None

                    for ts, speed in rows:
                        if previous is not None:
                            gap = ts - previous[0]
                            if 0 < gap <= 45:
                                total += max(0.0, speed) * gap
                                covered += gap
                        previous = (ts, speed)

                    item[f"{direction}_bytes"] = round(total)
                    item[f"{direction}_covered_seconds"] = covered
                    item[f"{direction}_first_sample"] = (
                        rows[0][0] if rows else None
                    )

                result[name] = item
        finally:
            conn.close()

    except Exception:
        raise HTTPException(503, "network usage history unavailable")

    return {
        "date": now.date().isoformat(),
        "timezone": "Asia/Colombo",
        "estimated": True,
        "interfaces": result,
    }


@app.get("/api/reliability", dependencies=[Depends(require_login)])
def reliability():
    """Sanitized, read-only host reliability snapshot."""
    expected = (
        "server-monitor", "imfs-pg-m1", "imfs-pg-m2",
        "imfs-pg-m3", "imfs-pg-m4", "imfs-pg-integration",
        "homelab-test", "homelab-ollama",
    )
    roles = container_roles(expected)
    try:
        snapshot = json.loads(Path("/data/activity/latest.json").read_text())
        timestamp = int(snapshot["timestamp"])
        age = int(time.time()) - timestamp
        if age < 0 or age > 90:
            raise ValueError("stale snapshot")

        raw_docker = snapshot.get("docker") or {}
        containers = {}
        for name in expected:
            item = (raw_docker.get("containers") or {}).get(name) or {}
            state = item.get("state")
            health = item.get("health")
            restarts = item.get("restarts")
            started_at = item.get("started_at")
            safe_state = state if state in (
                "running", "exited", "restarting", "paused",
                "created", "dead", "removing"
            ) else "unknown"
            containers[name] = {
                "state": safe_state,
                "health": health if health in (
                    "healthy", "unhealthy", "starting", "none"
                ) else "unknown",
                "restarts": restarts if (
                    type(restarts) is int and 0 <= restarts <= 1000000
                ) else None,
                "uptime_seconds": calculate_uptime_seconds(
                    safe_state, started_at, time.time()
                ),
                **roles[name],
            }

        raw_storage = snapshot.get("storage") or {}
        storage = {"status": "unknown"}
        if raw_storage.get("status") == "ok":
            values = [
                raw_storage.get(k) for k in
                ("total_bytes", "used_bytes", "available_bytes")
            ]
            percent = raw_storage.get("used_percent")
            if (
                all(type(v) is int and v >= 0 for v in values)
                and type(percent) in (int, float)
                and 0 <= percent <= 100
            ):
                storage = {
                    "status": "ok",
                    "total_bytes": values[0],
                    "used_bytes": values[1],
                    "available_bytes": values[2],
                    "used_percent": percent,
                    "level": (
                        "critical" if percent >= 90 else
                        "warning" if percent >= 80 else "normal"
                    ),
                }

        network = snapshot.get("network") or {}
        preferred = network.get("preferred_interface")
        if preferred not in ("wlp2s0", "enp1s0"):
            preferred = None

        connectivity = network.get("connectivity") or {}
        def safe_status(name):
            status = (connectivity.get(name) or {}).get("status")
            return status if status in (
                "reachable", "unreachable", "http_error", "unknown"
            ) else "unknown"

        starlink = safe_status("starlink")
        dialog = safe_status("dialog")
        failover = {
            "status": (
                "primary_preferred" if preferred == "wlp2s0" else
                "backup_preferred" if preferred == "enp1s0" else "unknown"
            ),
            "preferred_interface": preferred,
            "wifi_ssid": network.get("wifi_ssid"),
            "interface_connectivity": network.get(
                "interface_connectivity", {}
            ),
            "starlink": starlink,
            "dialog": dialog,
            "backup_internet_confirmed": (
                preferred == "enp1s0" and dialog == "reachable"
            ),
            "active_checked_at": network.get("active_checked_at"),
            "backup_checked_at": network.get("backup_checked_at"),
        }

        return {
            "status": "ok",
            "timestamp": timestamp,
            "age_seconds": age,
            "docker": {
                # Preserve collector completeness: ok | partial | unknown.
                "status": (
                    raw_docker.get("status")
                    if raw_docker.get("status") in ("ok", "partial")
                    else "unknown"
                ),
                "containers": containers,
            },
            "storage": storage,
            "failover": failover,
        }
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return {
            "status": "unknown",
            "docker": {"status": "unknown", "containers": {}},
            "storage": {"status": "unknown"},
            "failover": {"status": "unknown"},
        }


@app.get("/api/alerts/history", dependencies=[Depends(require_login)])
def alert_history():
    """Read sanitized Telegram alert history without modifying alert state."""
    from collections import deque
    from datetime import datetime

    path = Path("/data/alerts/events.log")
    events = []

    try:
        if not path.exists():
            return {"status": "ok", "events": []}

        with path.open("r", encoding="utf-8", errors="replace") as file:
            recent = deque(file, maxlen=500)

        for line in reversed(recent):
            if len(events) >= 50:
                break

            line = line.strip()
            if len(line) > 1000:
                continue

            timestamp, separator, message = line.partition(" ")
            if not separator:
                continue

            try:
                parsed = datetime.fromisoformat(timestamp)
                if parsed.tzinfo is None:
                    continue
            except ValueError:
                continue

            if message.startswith("Queued: "):
                delivery = "queued"
                content = message[len("Queued: "):]
            elif message.startswith("Delivered: "):
                delivery = "delivered"
                content = message[len("Delivered: "):]
            else:
                continue

            severity = None
            for level in ("INFO", "WARNING", "CRITICAL"):
                prefix = f"[{level}] "
                if content.startswith(prefix):
                    severity = level.lower()
                    content = content[len(prefix):]
                    break

            if severity is None or not content or len(content) > 400:
                continue

            if any(ord(char) < 32 for char in content):
                continue

            events.append({
                "timestamp": parsed.isoformat(),
                "severity": severity,
                "delivery": delivery,
                "message": content,
            })

        return {"status": "ok", "events": events}

    except (OSError, ValueError):
        return {"status": "unknown", "events": []}


@app.get("/api/postgres/health", dependencies=[Depends(require_login)])
def postgres_health():
    """Return sanitized PostgreSQL health from the host collector."""
    expected = (
        "imfs-pg-m1",
        "imfs-pg-m2",
        "imfs-pg-m3",
        "imfs-pg-m4",
        "imfs-pg-integration",
    )
    roles = container_roles(expected)

    try:
        snapshot = json.loads(
            Path("/data/activity/latest.json").read_text()
        )
        age = int(time.time()) - int(snapshot["timestamp"])

        if age < 0 or age > 90:
            raise ValueError("stale snapshot")

        source = snapshot["databases"]
        if not isinstance(source, dict) or set(source) != set(expected):
            raise ValueError("invalid database snapshot")

        databases = {}
        for name in expected:
            databases[name] = sanitize_postgres_database(
                source[name], roles[name]
            )

        return {
            "status": "ok",
            "age_seconds": age,
            "databases": databases,
        }

    except (OSError, ValueError, KeyError, TypeError):
        return {
            "status": "unknown",
            "databases": {},
        }
