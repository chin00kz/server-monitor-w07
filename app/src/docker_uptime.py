"""Validation and calculation helpers for Docker container uptime."""

from datetime import datetime


ACTIVE_STATES = frozenset(("running", "restarting"))


def calculate_uptime_seconds(state, started_at, now):
    """Return uptime since Docker's StartedAt, or None for unsafe data."""
    if state not in ACTIVE_STATES or not isinstance(started_at, str):
        return None

    try:
        started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        if started.tzinfo is None:
            return None
        seconds = int(now - started.timestamp())
    except (OverflowError, TypeError, ValueError):
        return None

    return seconds if seconds >= 0 else None
