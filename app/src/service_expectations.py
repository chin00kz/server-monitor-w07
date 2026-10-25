"""Parse allowlisted service expectations from deployment configuration."""

import os


def _configured_names(variable, allowed, environ):
    raw = environ.get(variable, "")
    requested = {name.strip() for name in raw.split(",") if name.strip()}
    return requested.intersection(allowed)


def container_roles(names, environ=None):
    """Return expected/critical flags without trusting arbitrary names."""
    environ = os.environ if environ is None else environ
    allowed = set(names)
    critical = _configured_names(
        "DOCKER_CRITICAL_CONTAINERS", allowed, environ
    )
    expected = _configured_names(
        "DOCKER_EXPECTED_RUNNING", allowed, environ
    ).union(critical)
    return {
        name: {
            "expected_running": name in expected,
            "critical": name in critical,
        }
        for name in names
    }
