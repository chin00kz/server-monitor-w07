"""Fail-closed validation for host-produced backup status."""
import json


BACKUP_STATES = {"running", "successful", "failed", "unavailable"}


def unavailable_status():
    return {
        "status": "unavailable",
        "timestamp": None,
        "message": "Backup status is unavailable",
        "last_local": {"status": "unavailable", "timestamp": None},
        "last_cloud": {"status": "unavailable", "timestamp": None},
        "storage": {"status": "unavailable", "total": None, "used": None, "free": None},
    }


def load_backup_status(path, now_ts):
    try:
        source = json.loads(path.read_text())
        status = source["status"]
        timestamp = source["timestamp"]
        if (
            not isinstance(source, dict)
            or status not in BACKUP_STATES
            or type(timestamp) is not int
        ):
            raise ValueError("invalid backup status")
        age = now_ts - timestamp
        max_age = 7500 if status == "running" else 180
        if age < 0 or age > max_age:
            raise ValueError("stale backup status")

        result = unavailable_status()
        result["status"] = status
        result["timestamp"] = timestamp
        message = source.get("message")
        result["message"] = (
            message if isinstance(message, str) and 0 < len(message) <= 200
            else "Backup status updated"
        )

        for key in ("last_local", "last_cloud"):
            item = source.get(key)
            if not isinstance(item, dict):
                continue
            item_status = item.get("status")
            item_timestamp = item.get("timestamp")
            if (
                item_status in {"successful", "failed"}
                and type(item_timestamp) is int
                and 0 < item_timestamp <= now_ts
            ):
                result[key] = {"status": item_status, "timestamp": item_timestamp}

        storage = source.get("storage")
        if isinstance(storage, dict) and storage.get("status") == "ok":
            total, used, free = (
                storage.get("total"), storage.get("used"), storage.get("free")
            )
            if (
                all(type(value) is int and value >= 0 for value in (total, used, free))
                and total > 0 and used <= total and free <= total
            ):
                result["storage"] = {
                    "status": "ok", "total": total, "used": used, "free": free,
                }
        return result
    except (OSError, ValueError, TypeError, KeyError):
        return unavailable_status()
