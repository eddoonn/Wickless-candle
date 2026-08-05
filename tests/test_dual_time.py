from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from live_scan import dual_timezone_discord_payload
from scripts.rerun_production_backtest import enrich_report_times
from time_display import london_iso, utc_london_text


UTC = timezone.utc


class DualTimeTests(unittest.TestCase):
    def test_london_conversion_uses_bst_in_summer_and_gmt_in_winter(self) -> None:
        self.assertEqual(
            london_iso("2026-08-05T10:00:00+00:00"),
            "2026-08-05T11:00:00+01:00",
        )
        self.assertEqual(
            london_iso("2026-01-05T10:00:00+00:00"),
            "2026-01-05T10:00:00+00:00",
        )
        rendered = utc_london_text("2026-08-05T10:00:00+00:00")
        self.assertIn("UTC 2026-08-05T10:00:00+00:00 (UTC)", rendered)
        self.assertIn("London 2026-08-05T11:00:00+01:00 (BST)", rendered)

    def test_live_payload_replaces_partial_time_fields_with_four_dual_time_fields(self) -> None:
        base = {
            "embeds": [
                {
                    "fields": [
                        {"name": "Actionability", "value": "ACTIONABLE"},
                        {"name": "Published (UTC)", "value": "old"},
                        {"name": "Reclaim entry time (London)", "value": "old"},
                        {"name": "Signal ID", "value": "abc"},
                    ],
                    "footer": {"text": "source"},
                }
            ]
        }
        signal = SimpleNamespace(
            signal_time_utc="2026-08-05T09:45:00+00:00",
            fill_time_utc="2026-08-05T10:00:00+00:00",
            detected_time_utc="2026-08-05T10:00:20+00:00",
            published_time_utc="2026-08-05T10:00:21+00:00",
        )
        with patch("live_scan._ORIGINAL_DISCORD_PAYLOAD", return_value=base):
            payload = dual_timezone_discord_payload(signal)
        fields = payload["embeds"][0]["fields"]
        names = [field["name"] for field in fields]
        self.assertNotIn("Published (UTC)", names)
        self.assertNotIn("Reclaim entry time (London)", names)
        self.assertEqual(
            names[-5:],
            [
                "Signal close time",
                "Entry time",
                "Detected time",
                "Published time",
                "Signal ID",
            ],
        )
        self.assertIn("2026-08-05T11:00:00+01:00 (BST)", fields[-4]["value"])
        self.assertIn("UTC and Europe/London", payload["embeds"][0]["footer"]["text"])

    def test_backtest_report_adds_london_windows_qa_and_trade_times(self) -> None:
        report = {
            "folds": {
                "june_2026": {
                    "window": {
                        "start_utc": "2026-06-01T00:00:00+00:00",
                        "end_utc_exclusive": "2026-07-01T00:00:00+00:00",
                    },
                    "data_qa": [
                        {
                            "first_bar_utc": "2026-05-27T00:00:00+00:00",
                            "last_bar_utc": "2026-06-30T23:45:00+00:00",
                        }
                    ],
                }
            },
            "trades": [
                {
                    "signal_time_utc": "2026-06-05T09:15:00+00:00",
                    "entry_time_utc": "2026-06-05T09:30:00+00:00",
                    "exit_time_utc": "2026-06-05T10:00:00+00:00",
                }
            ],
        }
        enriched = enrich_report_times(
            report,
            generated_at=datetime(2026, 8, 5, 10, 30, tzinfo=UTC),
        )
        self.assertEqual(
            enriched["generated_at_london"], "2026-08-05T11:30:00+01:00"
        )
        window = enriched["folds"]["june_2026"]["window"]
        self.assertEqual(window["start_london"], "2026-06-01T01:00:00+01:00")
        qa = enriched["folds"]["june_2026"]["data_qa"][0]
        self.assertEqual(qa["last_bar_london"], "2026-07-01T00:45:00+01:00")
        trade = enriched["trades"][0]
        self.assertEqual(trade["entry_time_london"], "2026-06-05T10:30:00+01:00")


if __name__ == "__main__":
    unittest.main()
