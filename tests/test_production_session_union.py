from __future__ import annotations

import unittest
from datetime import datetime, time, timezone
from pathlib import Path

import no_wick_research
import wickless_bot
from no_wick_research import NoWickConfig
from production_session import (
    SESSION_LABEL,
    in_production_session,
    install_production_session,
)
from wickless_bot import Bar


ROOT = Path(__file__).resolve().parents[1]
UTC = timezone.utc


def bar_at(value: str) -> Bar:
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    return Bar(timestamp=stamp, open=1.0, high=1.1, low=0.9, close=1.05)


class ProductionSessionUnionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = NoWickConfig()

    def test_summer_london_open_is_included_before_new_york(self) -> None:
        self.assertTrue(in_production_session(bar_at("2026-06-10T07:00:00Z"), self.config))

    def test_winter_london_open_is_included_before_new_york(self) -> None:
        self.assertTrue(in_production_session(bar_at("2026-01-05T08:00:00Z"), self.config))

    def test_late_new_york_period_is_included_after_london(self) -> None:
        self.assertTrue(in_production_session(bar_at("2026-06-10T17:15:00Z"), self.config))

    def test_before_both_sessions_is_excluded(self) -> None:
        self.assertFalse(in_production_session(bar_at("2026-06-10T06:45:00Z"), self.config))

    def test_union_end_is_exclusive(self) -> None:
        self.assertFalse(in_production_session(bar_at("2026-06-10T17:30:00Z"), self.config))

    def test_candidate_clock_fields_cannot_narrow_either_production_window(self) -> None:
        config = NoWickConfig(session_start=time(9, 30), session_end=time(10, 0))
        self.assertTrue(in_production_session(bar_at("2026-06-10T07:00:00Z"), config))
        self.assertTrue(in_production_session(bar_at("2026-06-10T17:15:00Z"), config))

    def test_all_hours_diagnostic_still_contains_both_sessions(self) -> None:
        config = NoWickConfig(use_session=False)
        self.assertTrue(in_production_session(bar_at("2026-06-10T02:00:00Z"), config))

    def test_shared_installer_updates_engine_and_live_labels(self) -> None:
        install_production_session()
        self.assertIs(no_wick_research._in_entry_session, in_production_session)
        self.assertTrue(wickless_bot._production_session_label_installed)

    def test_live_and_autoresearch_entrypoints_install_the_union(self) -> None:
        live = (ROOT / "live_scan.py").read_text(encoding="utf-8")
        package = (ROOT / "autoresearch" / "__init__.py").read_text(encoding="utf-8")
        self.assertIn("install_production_session()", live)
        self.assertIn("install_production_session()", package)
        self.assertIn("Europe/London", SESSION_LABEL)
        self.assertIn("America/New_York", SESSION_LABEL)

    def test_pine_strategy_uses_the_same_union(self) -> None:
        pine = (ROOT / "Wickless_Reversal_Strategy_v1_0.pine").read_text(
            encoding="utf-8"
        )
        self.assertIn('input.session("0800-1700"', pine)
        self.assertIn('input.session("0500-1330"', pine)
        self.assertIn('"Europe/London"', pine)
        self.assertIn('"America/New_York"', pine)
        self.assertIn("inSession = inLondonSession or inNewYorkSession", pine)


if __name__ == "__main__":
    unittest.main()
