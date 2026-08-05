from __future__ import annotations

import unittest
from datetime import datetime, timezone

from no_wick_research import NoWickConfig
from scripts.compare_session_windows import (
    all_hours,
    current_new_york_session,
    full_london_session,
    london_new_york_union,
)
from wickless_bot import Bar


UTC = timezone.utc


def bar_at(value: str) -> Bar:
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    return Bar(timestamp=stamp, open=1.0, high=1.1, low=0.9, close=1.05)


class SessionComparisonTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = NoWickConfig()

    def test_london_open_is_excluded_by_current_new_york_window(self) -> None:
        london_open_summer = bar_at("2026-06-10T07:00:00Z")
        self.assertFalse(current_new_york_session(london_open_summer, self.config))
        self.assertTrue(full_london_session(london_open_summer, self.config))
        self.assertTrue(london_new_york_union(london_open_summer, self.config))

    def test_london_open_is_dst_aware_in_winter(self) -> None:
        london_open_winter = bar_at("2026-01-05T08:00:00Z")
        self.assertFalse(current_new_york_session(london_open_winter, self.config))
        self.assertTrue(full_london_session(london_open_winter, self.config))
        self.assertTrue(london_new_york_union(london_open_winter, self.config))

    def test_union_keeps_late_new_york_period_after_london_close(self) -> None:
        late_new_york = bar_at("2026-06-10T17:15:00Z")
        self.assertFalse(full_london_session(late_new_york, self.config))
        self.assertTrue(current_new_york_session(late_new_york, self.config))
        self.assertTrue(london_new_york_union(late_new_york, self.config))

    def test_session_boundaries_are_start_inclusive_end_exclusive(self) -> None:
        before_london = bar_at("2026-06-10T06:45:00Z")
        london_close = bar_at("2026-06-10T16:00:00Z")
        self.assertFalse(full_london_session(before_london, self.config))
        self.assertFalse(full_london_session(london_close, self.config))

    def test_all_hours_is_diagnostic_only_and_always_accepts(self) -> None:
        self.assertTrue(all_hours(bar_at("2026-06-10T02:00:00Z"), self.config))


if __name__ == "__main__":
    unittest.main()
