"""Pure network-health helpers for the host collector.

Overall WAN health is independent of Tailscale. ISP labels require an
explicit SSID match; Wi-Fi association alone never implies internet reachability.
"""

WAN_INTERFACES = ("wlp2s0", "enp1s0")
ALL_INTERFACES = ("wlp2s0", "enp1s0", "tailscale0")
STARLINK_SSID = "starlink-mini"
DIALOG_SSID = "Dialog 4G 247"
UNKNOWN_PROBE = {"status": "unknown", "http_code": None, "response_ms": None}


def classify_wifi_isp(ssid):
    """Map a connected SSID to an ISP label, or unknown."""
    if ssid == STARLINK_SSID:
        return "starlink"
    if ssid == DIALOG_SSID:
        return "dialog"
    return "unknown"


def network_status_ok(preferred_interface, interfaces):
    """True when the preferred WAN interface was read successfully.

    Tailscale (and any other non-WAN device) must not gate overall status.
    This only means interface inventory for the preferred WAN path is usable;
    internet reachability remains a separate probe result.
    """
    if preferred_interface not in WAN_INTERFACES:
        return False
    if not isinstance(interfaces, dict):
        return False
    item = interfaces.get(preferred_interface)
    return isinstance(item, dict) and item.get("status") == "ok"


def isp_connectivity(ssid, wifi_result, ethernet_result):
    """Attach ISP labels using explicit SSIDs; keep interface probes elsewhere."""
    unknown = dict(UNKNOWN_PROBE)
    wifi = wifi_result if isinstance(wifi_result, dict) else unknown
    ethernet = ethernet_result if isinstance(ethernet_result, dict) else unknown
    isp = classify_wifi_isp(ssid)

    starlink = wifi if isp == "starlink" else unknown
    if ethernet.get("status") == "reachable":
        dialog = ethernet
    elif isp == "dialog":
        dialog = wifi
    else:
        dialog = ethernet

    return {"starlink": starlink, "dialog": dialog}
