"""Static contract checks for Backups UI click guards (no live requests)."""
import re
import unittest
from pathlib import Path


INDEX = Path(__file__).resolve().parents[1] / "app" / "src" / "web" / "index.html"


class BackupUiGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = INDEX.read_text(encoding="utf-8")

    def _handler(self, marker):
        start = self.source.index(marker)
        # Handlers are consecutive click listeners or followed by async poll helpers.
        candidates = [
            self.source.find('\n$("#', start + len(marker)),
            self.source.find("\nasync function", start + len(marker)),
            self.source.find("\nconst ", start + len(marker)),
            self.source.find("\nlet ", start + len(marker)),
        ]
        ends = [index for index in candidates if index != -1]
        nxt = min(ends) if ends else start + 1600
        return self.source[start:nxt]

    def test_backup_uses_window_confirm_before_fetch(self):
        handler = self._handler('$("#backup-now").addEventListener("click"')
        confirm_at = handler.index("window.confirm(")
        fetch_at = handler.index('fetch("/api/backups/run"')
        self.assertLess(confirm_at, fetch_at)
        self.assertIn("if (!window.confirm(", handler)
        self.assertIn("return;", handler[confirm_at:fetch_at])
        self.assertIn("backupRequestInFlight", handler)
        self.assertIn("Starting backup…", handler)
        self.assertIn("button.disabled = false", handler)

    def test_backup_inflight_blocks_repeated_clicks(self):
        self.assertIn(
            "if (backupRequestInFlight || button.disabled) return;",
            self.source,
        )
        self.assertIn(
            "button.disabled = backupRequestInFlight || state === \"running\"",
            self.source,
        )

    def test_oracle_prepare_has_no_generic_confirm_popup(self):
        handler = self._handler('$("#oracle-sync-prepare").addEventListener("click"')
        self.assertNotIn("window.confirm(", handler)
        self.assertNotIn("confirm(", handler)
        self.assertIn('fetch("/api/backups/oracle-sync/prepare"', handler)
        self.assertIn("oraclePrepareInFlight", handler)
        self.assertIn("Preparing…", handler)
        self.assertIn("button.disabled = false", handler)

    def test_oracle_typed_confirmation_phrase_preserved(self):
        self.assertIn("Type REPLACE-ORACLE-IMFS", self.source)
        self.assertIn('placeholder="REPLACE-ORACLE-IMFS"', self.source)
        confirm = self._handler('$("#oracle-sync-confirm-btn").addEventListener("click"')
        self.assertIn("confirmation: phrase", confirm)
        self.assertIn("oracleConfirmInFlight", confirm)
        self.assertIn("Confirming…", confirm)
        self.assertIn("button.disabled = false", confirm)
        self.assertNotIn("window.confirm(", confirm)

    def test_cancel_path_is_before_backup_request(self):
        """Cancelling window.confirm must not reach /api/backups/run."""
        handler = self._handler('$("#backup-now").addEventListener("click"')
        # Early-return after failed confirm, with no fetch between confirm and return.
        match = re.search(
            r"if \(!window\.confirm\([^)]*\)\) return;.*?fetch\(\"/api/backups/run\"",
            handler,
            re.S,
        )
        self.assertIsNotNone(match)
        between = match.group(0)
        self.assertIn("backupRequestInFlight = true", between)


if __name__ == "__main__":
    unittest.main()
