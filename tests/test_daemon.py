from __future__ import annotations

import unittest
from datetime import datetime, timezone

from run_daemon import seconds_until_next_scan


UTC = timezone.utc


class DaemonTimingTests(unittest.TestCase):
    def test_aligns_to_next_five_minute_close_with_grace(self) -> None:
        now = datetime(2026, 7, 30, 8, 3, 10, tzinfo=UTC)
        self.assertEqual(seconds_until_next_scan(now, grace_seconds=20), 130)

    def test_rolls_across_hour(self) -> None:
        now = datetime(2026, 7, 30, 8, 59, 50, tzinfo=UTC)
        self.assertEqual(seconds_until_next_scan(now, grace_seconds=20), 30)


if __name__ == "__main__":
    unittest.main()
