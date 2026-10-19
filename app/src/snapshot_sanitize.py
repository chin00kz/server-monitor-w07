"""Sanitize host activity snapshot fragments for the dashboard API."""

ACTIVITY_COUNT_KEYS = (
    "active",
    "idle",
    "idle_in_transaction",
    "other",
    "total",
)

POSTGRES_METRIC_KEYS = (
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


def sanitize_activity_database(item):
    """Return count dict for a healthy DB, or {status: unknown} when degraded.

    Healthy entries omit status to preserve the existing activity API contract.
    """
    if not isinstance(item, dict) or item.get("status") != "ok":
        return {"status": "unknown"}

    counts = {}
    for key in ACTIVITY_COUNT_KEYS:
        value = item.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return {"status": "unknown"}
        counts[key] = value
    return counts


def sanitize_postgres_database(item, roles):
    """Return ok metrics + roles, or unknown + roles when degraded."""
    if not isinstance(item, dict) or item.get("status") != "ok":
        return {"status": "unknown", **roles}

    safe = {}
    for field in POSTGRES_METRIC_KEYS:
        value = item.get(field)
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < 0
        ):
            return {"status": "unknown", **roles}
        safe[field] = value

    if safe["max_connections"] == 0:
        return {"status": "unknown", **roles}

    return {"status": "ok", **roles, **safe}
