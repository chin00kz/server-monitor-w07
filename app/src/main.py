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
from zoneinfo import ZoneInfo

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel

import store
from host import PROC, SYS, Collector

HASH_FILE = Path("/secrets/admin.hash")
WEB = Path(__file__).parent / "web"
SESSION_TTL, MAX_FAILS, FAIL_WINDOW = 7 * 86400, 5, 900
SAMPLE_SECONDS = int(os.environ.get("SAMPLE_SECONDS", "15"))
KEY_RE = re.compile(r"^[a-z0-9_.]{1,64}$")

latest = {"ts": None, "m": {}, "info": {}}
ph = PasswordHasher()
sessions: dict[str, float] = {}
fails: dict[str, list[float]] = {}


def collector_loop():
    col = Collector()
    conn = store.connect()
    n, last_rollup = 0, 0.0
    while True:
        started = time.time()
        try:
            ts, metrics, info = col.sample(slow=(n % 2 == 0))
            store.insert(conn, ts, metrics)
            latest.update(ts=ts, m={**latest["m"], **metrics}, info=info)
            if started - last_rollup > 3600:
                store.rollup(conn)
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



@app.get("/api/activity", dependencies=[Depends(require_login)])
def activity():
    """Return sanitized PostgreSQL activity, rejecting stale snapshots."""
    path = Path("/data/activity/latest.json")
    now_ts = int(time.time())

    try:
        snapshot = json.loads(path.read_text())
        timestamp = int(snapshot["timestamp"])
        databases = snapshot["databases"]

        age = now_ts - timestamp
        expected = {
            "imfs-pg-m1", "imfs-pg-m2", "imfs-pg-m3",
            "imfs-pg-m4", "imfs-pg-integration",
        }

        if age < 0 or age > 90 or set(databases) != expected:
            raise ValueError("stale or incomplete snapshot")

        safe_databases = {}
        for name in sorted(expected):
            item = databases[name]
            if item.get("status") != "ok":
                raise ValueError("database check unavailable")

            counts = {}
            for key in ("active", "idle", "idle_in_transaction", "other", "total"):
                value = item.get(key)
                if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                    raise ValueError("invalid activity count")
                counts[key] = value

            safe_databases[name] = counts

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

        names = ("wlp2s0", "enp1s0", "tailscale0")
        if isinstance(raw_network, dict):
            raw_interfaces = raw_network.get("interfaces")
            if isinstance(raw_interfaces, dict):
                for name in names:
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

            preferred = raw_network.get("preferred_interface")
            if preferred in names:
                safe_network["preferred_interface"] = preferred

            if (
                raw_network.get("status") == "ok"
                and len(safe_network["interfaces"]) == len(names)
                and safe_network["preferred_interface"] is not None
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
    )
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
            containers[name] = {
                "state": state if state in (
                    "running", "exited", "restarting", "paused",
                    "created", "dead", "removing"
                ) else "unknown",
                "health": health if health in (
                    "healthy", "unhealthy", "starting", "none"
                ) else "unknown",
                "restarts": restarts if (
                    type(restarts) is int and 0 <= restarts <= 1000000
                ) else None,
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
                "status": (
                    "ok" if raw_docker.get("status") == "ok"
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

        fields = (
            "active",
            "idle",
            "idle_in_transaction",
            "other",
            "total",
            "long_running",
            "max_connections",
            "database_size_bytes",
            "response_ms",
        )

        for name in expected:
            item = source[name]

            if not isinstance(item, dict) or item.get("status") != "ok":
                databases[name] = {"status": "unknown"}
                continue

            safe = {}
            for field in fields:
                value = item.get(field)

                if (
                    not isinstance(value, int)
                    or isinstance(value, bool)
                    or value < 0
                ):
                    raise ValueError("invalid PostgreSQL metric")

                safe[field] = value

            if safe["max_connections"] == 0:
                raise ValueError("invalid connection capacity")

            databases[name] = {"status": "ok", **safe}

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
