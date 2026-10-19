"""Pure network-health helpers for the dashboard API.

Keep behaviour aligned with activity/network_health.py (host collector).
Overall WAN health is independent of Tailscale. ISP labels are assigned by
the collector from explicit SSIDs; this module only decides overall status.
"""

WAN_INTERFACES = ("wlp2s0", "enp1s0")
ALL_INTERFACES = ("wlp2s0", "enp1s0", "tailscale0")


def network_status_ok(preferred_interface, interfaces):
    """True when the preferred WAN interface was read successfully.

    Tailscale must not gate overall status. Internet reachability remains a
    separate probe field on the snapshot.
    """
    if preferred_interface not in WAN_INTERFACES:
        return False
    if not isinstance(interfaces, dict):
        return False
    item = interfaces.get(preferred_interface)
    return isinstance(item, dict) and item.get("status") == "ok"
