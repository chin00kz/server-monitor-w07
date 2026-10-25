import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path


APP_SRC = Path(__file__).resolve().parents[1] / "app" / "src"
sys.path.insert(0, str(APP_SRC))

from docker_uptime import calculate_uptime_seconds  # noqa: E402


class DockerUptimeTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc).timestamp()

    def test_running_uptime_uses_started_at(self):
        uptime = calculate_uptime_seconds(
            "running", "2026-10-12T21:27:30.000000000Z", self.now
        )
        self.assertEqual(uptime, 138750)

    def test_stopped_container_has_no_uptime(self):
        self.assertIsNone(calculate_uptime_seconds(
            "exited", "2026-10-12T21:27:30Z", self.now
        ))

    def test_later_started_at_resets_uptime_after_restart(self):
        before = calculate_uptime_seconds(
            "running", "2026-10-12T12:00:00Z", self.now
        )
        after = calculate_uptime_seconds(
            "running", "2026-10-14T11:55:00Z", self.now
        )
        self.assertEqual(before, 172800)
        self.assertEqual(after, 300)

    def test_restarting_container_uses_latest_start(self):
        self.assertEqual(calculate_uptime_seconds(
            "restarting", "2026-10-14T11:59:30Z", self.now
        ), 30)

    def test_missing_invalid_naive_or_future_timestamp_is_unavailable(self):
        for started_at in (None, "", "not-a-date", "2026-10-14T11:00:00"):
            with self.subTest(started_at=started_at):
                self.assertIsNone(calculate_uptime_seconds(
                    "running", started_at, self.now
                ))
        self.assertIsNone(calculate_uptime_seconds(
            "running", "2026-10-14T12:00:01Z", self.now
        ))


if __name__ == "__main__":
    unittest.main()
