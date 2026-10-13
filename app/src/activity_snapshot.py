"""Validation and sanitization for the host activity snapshot."""
import json


ACTIVITY_DATABASES = {
    "imfs-pg-m1", "imfs-pg-m2", "imfs-pg-m3",
    "imfs-pg-m4", "imfs-pg-integration",
}


def load_activity_snapshot(path, now_ts):
    snapshot = json.loads(path.read_text())
    timestamp = snapshot["timestamp"]
    databases = snapshot["databases"]

    if (
        not isinstance(timestamp, int)
        or isinstance(timestamp, bool)
        or not isinstance(databases, dict)
    ):
        raise ValueError("invalid snapshot")

    age = now_ts - timestamp
    if age < 0 or age > 90 or set(databases) != ACTIVITY_DATABASES:
        raise ValueError("stale or incomplete snapshot")

    safe_databases = {}
    for name in sorted(ACTIVITY_DATABASES):
        item = databases[name]
        if not isinstance(item, dict):
            raise ValueError("invalid database entry")

        # A failed probe is local to that database. Docker state is not a
        # validated input here, so a failed probe must remain "unknown".
        if item.get("status") != "ok":
            safe_databases[name] = {"status": "unknown"}
            continue

        counts = {}
        for key in ("active", "idle", "idle_in_transaction", "other", "total"):
            value = item.get(key)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                counts = None
                break
            counts[key] = value

        safe_databases[name] = (
            {"status": "ok", **counts}
            if counts is not None
            else {"status": "unknown"}
        )

    return snapshot, timestamp, age, safe_databases
