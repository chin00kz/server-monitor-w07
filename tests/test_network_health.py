import importlib.util
import os
import unittest


ROOT = os.path.join(os.path.dirname(__file__), "..")
ACTIVITY = os.path.join(ROOT, "activity")
APP_SRC = os.path.join(ROOT, "app", "src")


def _load(module_name, path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


collector_health = _load(
    "collector_network_health",
    os.path.join(ACTIVITY, "network_health.py"),
)
api_health = _load(
    "api_network_health",
    os.path.join(APP_SRC, "network_health.py"),
)


def _iface(status="ok", operstate="UP", ipv4="192.168.1.10"):
    return {"status": status, "operstate": operstate, "ipv4": ipv4}


class NetworkStatusConsistencyTests(unittest.TestCase):
    def test_api_and_collector_agree_on_wan_ok_without_tailscale(self):
        interfaces = {
            "wlp2s0": _iface(),
            "enp1s0": _iface(status="unknown", operstate=None, ipv4=None),
            "tailscale0": _iface(status="unknown", operstate=None, ipv4=None),
        }
        self.assertTrue(
            collector_health.network_status_ok("wlp2s0", interfaces)
        )
        self.assertTrue(api_health.network_status_ok("wlp2s0", interfaces))

    def test_missing_tailscale_does_not_invalidate_wan(self):
        interfaces = {"wlp2s0": _iface(), "enp1s0": _iface()}
        self.assertTrue(
            collector_health.network_status_ok("wlp2s0", interfaces)
        )
        self.assertTrue(api_health.network_status_ok("wlp2s0", interfaces))

    def test_preferred_tailscale_is_not_overall_ok(self):
        interfaces = {
            "wlp2s0": _iface(),
            "enp1s0": _iface(),
            "tailscale0": _iface(ipv4="100.98.191.25"),
        }
        self.assertFalse(
            collector_health.network_status_ok("tailscale0", interfaces)
        )
        self.assertFalse(
            api_health.network_status_ok("tailscale0", interfaces)
        )

    def test_missing_preferred_wan_inventory_is_not_ok(self):
        interfaces = {
            "wlp2s0": _iface(status="unknown", operstate=None, ipv4=None),
            "enp1s0": _iface(),
        }
        self.assertFalse(
            collector_health.network_status_ok("wlp2s0", interfaces)
        )


class IspClassificationTests(unittest.TestCase):
    def test_explicit_starlink_ssid(self):
        self.assertEqual(
            collector_health.classify_wifi_isp("starlink-mini"), "starlink"
        )

    def test_explicit_dialog_ssid(self):
        self.assertEqual(
            collector_health.classify_wifi_isp("Dialog 4G 247"), "dialog"
        )

    def test_unknown_or_null_ssid(self):
        self.assertEqual(collector_health.classify_wifi_isp(None), "unknown")
        self.assertEqual(collector_health.classify_wifi_isp("guest"), "unknown")

    def test_null_ssid_does_not_label_starlink_but_keeps_interface_probe(self):
        wifi = {"status": "unreachable", "http_code": None, "response_ms": None}
        ethernet = {
            "status": "unknown", "http_code": None, "response_ms": None
        }
        connectivity = collector_health.isp_connectivity(None, wifi, ethernet)
        self.assertEqual(connectivity["starlink"]["status"], "unknown")
        self.assertEqual(connectivity["dialog"], ethernet)

    def test_starlink_ssid_maps_wifi_probe_to_starlink_label(self):
        wifi = {"status": "reachable", "http_code": 200, "response_ms": 40}
        ethernet = {
            "status": "unreachable", "http_code": None, "response_ms": None
        }
        connectivity = collector_health.isp_connectivity(
            "starlink-mini", wifi, ethernet
        )
        self.assertEqual(connectivity["starlink"], wifi)
        self.assertEqual(connectivity["dialog"], ethernet)

    def test_dialog_ssid_maps_wifi_probe_when_ethernet_not_reachable(self):
        wifi = {"status": "reachable", "http_code": 200, "response_ms": 55}
        ethernet = {
            "status": "unreachable", "http_code": None, "response_ms": None
        }
        connectivity = collector_health.isp_connectivity(
            "Dialog 4G 247", wifi, ethernet
        )
        self.assertEqual(connectivity["starlink"]["status"], "unknown")
        self.assertEqual(connectivity["dialog"], wifi)


if __name__ == "__main__":
    unittest.main()
