from __future__ import annotations

import csv
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from live_data import (
    LIVE_LOOKBACK_MINUTES,
    aggregate_fifteen_minutes,
    decode_minute_candles,
    fetch_current_day,
    refresh,
)
from wickless_bot import INSTRUMENTS


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

    def test_live_fetch_uses_two_week_strategy_reconstruction_window(self) -> None:
        self.assertEqual(LIVE_LOOKBACK_MINUTES, 14 * 24 * 60)
        now = datetime(2026, 7, 30, 0, 30, tzinfo=UTC)
        start = now - timedelta(minutes=LIVE_LOOKBACK_MINUTES)
        payload = {
            "timestamp": int(start.timestamp() * 1000),
            "shift": 1,
            "multiplier": 0.00001,
            "open": 1.1,
            "high": 1.1,
            "low": 1.1,
            "close": 1.1,
            "times": [60_000],
            "opens": [0],
            "highs": [1],
            "lows": [-1],
            "closes": [0],
        }
        with patch("live_data._request_json", return_value=payload) as request:
            candles = fetch_current_day(
                INSTRUMENTS["eurusd"], now=now, side="ASK"
            )
        query = parse_qs(urlparse(request.call_args.args[0]).query)
        self.assertEqual(query["from"], [str(int(start.timestamp() * 1000))])
        self.assertIn("/ASK?", request.call_args.args[0])
        self.assertEqual(len(candles), 1)

    def test_aggregates_true_fifteen_minute_ohlc(self) -> None:
        candles = [
            {
                "timestamp": 900_000,
                "open": 10.0,
                "high": 11.0,
                "low": 9.5,
                "close": 10.5,
            },
            {
                "timestamp": 960_000,
                "open": 10.5,
                "high": 12.0,
                "low": 10.0,
                "close": 11.5,
            },
            {
                "timestamp": 1_800_000,
                "open": 11.5,
                "high": 11.7,
                "low": 11.0,
                "close": 11.2,
            },
        ]
        aggregated = aggregate_fifteen_minutes(candles)
        self.assertEqual(
            aggregated,
            [
                {
                    "timestamp": 900_000,
                    "open": 10.0,
                    "high": 12.0,
                    "low": 9.5,
                    "close": 11.5,
                },
                {
                    "timestamp": 1_800_000,
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
            self.assertTrue((root / "eurusd-m15-ask-live.csv").exists())
            quote = json.loads(
                (root / "eurusd-quote-live.json").read_text(encoding="utf-8")
            )
            self.assertEqual(quote["source"], "Dukascopy Jetta BID/ASK minute candles")

    def test_refresh_reports_partial_failure_without_hiding_market(self) -> None:
        now = datetime(2026, 7, 30, 8, 10, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as directory:
            with patch(
                "live_data.fetch_current_day",
                side_effect=RuntimeError("feed down"),
            ):
                with self.assertRaisesRegex(RuntimeError, "no complete BID/ASK markets"):
                    refresh(
                        Path(directory),
                        instruments=["eurusd"],
                        now=now,
                        workers=1,
                    )

    def test_refresh_keeps_healthy_markets_when_one_market_fails(self) -> None:
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

        def fetch(profile, *, now, side):
            if profile.key == "gbpusd" and side == "ASK":
                raise RuntimeError("ask feed down")
            return current

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            status_output = root / "live-data-status.json"
            with patch("live_data.fetch_current_day", side_effect=fetch):
                outputs = refresh(
                    root,
                    instruments=["eurusd", "gbpusd"],
                    now=now,
                    workers=1,
                    status_output=status_output,
                )
            self.assertEqual(set(outputs), {"eurusd"})
            status = json.loads(status_output.read_text(encoding="utf-8"))
            self.assertEqual(status["requested_instruments"], ["eurusd", "gbpusd"])
            self.assertEqual(status["successful_instruments"], ["eurusd"])
            self.assertIn("gbpusd", status["failed_instruments"])
            self.assertTrue((root / "eurusd-quote-live.json").exists())
            self.assertFalse((root / "gbpusd-quote-live.json").exists())


if __name__ == "__main__":
    unittest.main()
