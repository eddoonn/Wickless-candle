from __future__ import annotations

import csv
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from live_data import aggregate_five_minutes, decode_minute_candles, refresh


UTC = timezone.utc


class LiveDataTests(unittest.TestCase):
    def test_delta_decoder_reconstructs_prices_and_times(self) -> None:
        payload = {
            "timestamp": 0,
            "shift": 1,
            "multiplier": 0.001,
            "open": 100.0,
            "high": 100.0,
            "low": 100.0,
            "close": 100.0,
            "times": [1000, 60_000],
            "opens": [1000, -500],
            "highs": [1000, -500],
            "lows": [1000, -500],
            "closes": [1000, -500],
        }
        candles = decode_minute_candles(payload)
        self.assertEqual(candles[0]["timestamp"], 1000)
        self.assertEqual(candles[0]["open"], 101.0)
        self.assertEqual(candles[1]["timestamp"], 61_000)
        self.assertEqual(candles[1]["close"], 100.5)

    def test_inconsistent_live_arrays_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            decode_minute_candles(
                {
                    "times": [1],
                    "opens": [],
                    "highs": [],
                    "lows": [],
                    "closes": [],
                }
            )

    def test_aggregates_true_five_minute_ohlc(self) -> None:
        candles = [
            {
                "timestamp": 300_000,
                "open": 10.0,
                "high": 11.0,
                "low": 9.5,
                "close": 10.5,
            },
            {
                "timestamp": 360_000,
                "open": 10.5,
                "high": 12.0,
                "low": 10.0,
                "close": 11.5,
            },
            {
                "timestamp": 600_000,
                "open": 11.5,
                "high": 11.7,
                "low": 11.0,
                "close": 11.2,
            },
        ]
        aggregated = aggregate_five_minutes(candles)
        self.assertEqual(
            aggregated,
            [
                {
                    "timestamp": 300_000,
                    "open": 10.0,
                    "high": 12.0,
                    "low": 9.5,
                    "close": 11.5,
                },
                {
                    "timestamp": 600_000,
                    "open": 11.5,
                    "high": 11.7,
                    "low": 11.0,
                    "close": 11.2,
                },
            ],
        )

    def test_refresh_writes_each_requested_market_atomically(self) -> None:
        now = datetime(2026, 7, 30, 8, 10, tzinfo=UTC)
        current = [
            {
                "timestamp": int(now.timestamp() * 1000),
                "open": 1.0,
                "high": 1.1,
                "low": 0.9,
                "close": 1.05,
            }
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("live_data.fetch_current_day", return_value=current):
                outputs = refresh(
                    root,
                    instruments=["eurusd", "gbpusd"],
                    now=now,
                    workers=2,
                )
            self.assertEqual(set(outputs), {"eurusd", "gbpusd"})
            for output in outputs.values():
                with output.open(newline="", encoding="utf-8") as handle:
                    rows = list(csv.DictReader(handle))
                self.assertEqual(len(rows), 1)
                self.assertFalse(output.with_suffix(".csv.tmp").exists())

    def test_refresh_reports_partial_failure_without_hiding_market(self) -> None:
        now = datetime(2026, 7, 30, 8, 10, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as directory:
            with patch(
                "live_data.fetch_current_day",
                side_effect=RuntimeError("feed down"),
            ):
                with self.assertRaisesRegex(RuntimeError, "eurusd"):
                    refresh(
                        Path(directory),
                        instruments=["eurusd"],
                        now=now,
                        workers=1,
                    )


if __name__ == "__main__":
    unittest.main()
