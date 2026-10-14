import sys
import unittest
from pathlib import Path


APP_SRC = Path(__file__).resolve().parents[1] / "app" / "src"
sys.path.insert(0, str(APP_SRC))

from service_expectations import container_roles  # noqa: E402


class ServiceExpectationTests(unittest.TestCase):
    def test_empty_configuration_makes_no_runtime_assumptions(self):
        roles = container_roles(("api", "database"), {})

        self.assertEqual(roles, {
            "api": {"expected_running": False, "critical": False},
            "database": {"expected_running": False, "critical": False},
        })

    def test_critical_services_are_also_expected(self):
        roles = container_roles(("api", "database"), {
            "DOCKER_EXPECTED_RUNNING": "api",
            "DOCKER_CRITICAL_CONTAINERS": "database",
        })

        self.assertEqual(roles["api"], {
            "expected_running": True, "critical": False,
        })
        self.assertEqual(roles["database"], {
            "expected_running": True, "critical": True,
        })

    def test_unknown_names_are_ignored(self):
        roles = container_roles(("api",), {
            "DOCKER_EXPECTED_RUNNING": "api,other",
            "DOCKER_CRITICAL_CONTAINERS": "missing",
        })

        self.assertEqual(set(roles), {"api"})
        self.assertTrue(roles["api"]["expected_running"])
        self.assertFalse(roles["api"]["critical"])


if __name__ == "__main__":
    unittest.main()
