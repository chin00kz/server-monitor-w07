"""Fail-closed validation for host-produced Oracle sync status."""
import json
import re


ORACLE_SYNC_STATES = {
    "idle",
    "preparing",
    "awaiting_confirmation",
    "applying",
    "successful",
    "failed",
    "cancelled",
    "unavailable",
}
REQUEST_RE = re.compile(r"^[a-f0-9]{32}$")
HASH_RE = re.compile(r"^[a-f0-9]{64}$")
DB_NAMES = ("imfs_central", "imfs_tenant_apex")
LOG_RETURN_CHARS = 64_000


def _base_oracle_sync(status, message, *, live_enabled=False):
    return {
        "status": status,
        "timestamp": None,
        "message": message,
        "request_id": None,
        "started_at": None,
        "finished_at": None,
        "duration_seconds": None,
        "exit_status": None,
        "dump_hashes": {},
        "log": "",
        "live_enabled": bool(live_enabled),
    }


def unavailable_oracle_sync(*, live_enabled=False):
    return _base_oracle_sync(
        "unavailable",
        "Oracle sync status is unavailable",
        live_enabled=live_enabled,
    )


def idle_oracle_sync(*, live_enabled=False):
    """Valid initial state when no sync job has written status yet."""
    return _base_oracle_sync(
        "idle",
        "No Oracle sync has been run yet.",
        live_enabled=live_enabled,
    )


def _safe_int(value):
    return value if type(value) is int else None


def load_oracle_sync_log(path, limit=LOG_RETURN_CHARS):
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if len(text) <= limit:
        return text
    return text[-limit:]


def load_oracle_sync_status(status_path, log_path, now_ts, *, live_enabled=False):
    # Missing files mean "never run", not a broken controller. Do not require the
    # host oneshot to pre-create idle status before the first prepare.
    if not status_path.is_file():
        return idle_oracle_sync(live_enabled=live_enabled)

    result = unavailable_oracle_sync(live_enabled=live_enabled)
    try:
        source = json.loads(status_path.read_text(encoding="utf-8"))
        if not isinstance(source, dict):
            raise ValueError("invalid oracle sync status")
        status = source.get("status")
        timestamp = source.get("timestamp")
        if status not in ORACLE_SYNC_STATES or type(timestamp) is not int:
            raise ValueError("invalid oracle sync status")

        age = now_ts - timestamp
        if status in {"preparing", "applying"}:
            max_age = 7500
        elif status == "awaiting_confirmation":
            max_age = 86400
        else:
            max_age = 86400
        if age < 0 or age > max_age:
            raise ValueError("stale oracle sync status")

        result["status"] = status
        result["timestamp"] = timestamp
        message = source.get("message")
        result["message"] = (
            message if isinstance(message, str) and 0 < len(message) <= 300
            else "Oracle sync status updated"
        )

        request_id = source.get("request_id")
        if isinstance(request_id, str) and REQUEST_RE.fullmatch(request_id):
            result["request_id"] = request_id

        for key in ("started_at", "finished_at", "exit_status", "duration_seconds"):
            result[key] = _safe_int(source.get(key))

        hashes = source.get("dump_hashes")
        safe_hashes = {}
        if isinstance(hashes, dict):
            for name in DB_NAMES:
                value = hashes.get(name)
                if isinstance(value, str) and HASH_RE.fullmatch(value):
                    safe_hashes[name] = value
        result["dump_hashes"] = safe_hashes
        result["log"] = load_oracle_sync_log(log_path)
        return result
    except (OSError, ValueError, TypeError, KeyError):
        return result
