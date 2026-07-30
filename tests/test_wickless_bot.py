from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from wickless_bot import (
    Bar,
    FOREX_MAJORS,
    INSTRUMENTS,
    LIVE_INSTRUMENTS,
    StrategyConfig,
    build_signal,
    classify_wickless,
    discord_payload,
    find_fresh_signals,
    load_bars,
    run_backtest,
    scan_markets,
    validate_webhook_url,
)


UTC = timezone.utc


def bar(
    iso_time: str,
    open_: float,
    high: float,
    low: float,
    close: float,
) -> Bar:
    return Bar(
        timestamp=datetime.fromisoformat(iso_time).replace(tzinfo=UTC),
        open=open_,
        high=high,
        low=low,
        close=close,
    )


def write_csv(path: Path, bars: list[Bar]) -> None:
    rows = ["timestamp,open,high,low,close"]
    rows.extend(
        f"{int(item.timestamp.timestamp() * 1000)},"
        f"{item.open},{item.high},{item.low},{item.close}"
        for item in bars
    )
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


class WicklessDetectionTests(unittest.TestCase):
    def test_bullish_open_equals_low_is_missing_lower_wick(self) -> None:
        pattern = classify_wickless(
            bar("2026-07-30T08:00:00", 1.1000, 1.1010, 1.1000, 1.1008),
            tick_size=0.00001,
        )
        self.assertIsNotNone(pattern)
        assert pattern is not None
        self.assertEqual(pattern.kind, "BULLISH_WICKLESS")
        self.assertEqual(pattern.missing_wick, "LOWER")
        self.assertEqual(pattern.signal_side, "BUY")

    def test_bearish_open_equals_high_is_missing_upper_wick(self) -> None:
        pattern = classify_wickless(
            bar("2026-07-30T08:00:00", 1.1000, 1.1000, 1.0990, 1.0992),
            tick_size=0.00001,
        )
        self.assertIsNotNone(pattern)
        assert pattern is not None
        self.assertEqual(pattern.kind, "BEARISH_WICKLESS")
        self.assertEqual(pattern.signal_side, "SELL")

    def test_usdcad_bullish_wickless_is_buy_at_two_r(self) -> None:
        """Regression: a green USDCAD wickless marker maps to a 2R BUY."""
        signal = build_signal(
            bar(
                "2026-07-30T12:45:00",
                1.40415,
                1.40474,
                1.40415,
                1.40447,
            ),
            StrategyConfig(instrument="usdcad"),
        )
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.pattern, "BULLISH_WICKLESS")
        self.assertEqual(signal.side, "BUY")
        self.assertEqual(signal.entry_reference, 1.40447)
        self.assertEqual(signal.stop, 1.40395)
        self.assertEqual(signal.target, 1.40551)
        self.assertNotEqual(signal.key, "43336fd0095537a4")

    def test_half_tick_tolerance_accepts_only_rounding_noise(self) -> None:
        accepted = bar(
            "2026-07-30T08:00:00",
            1.100005,
            1.1010,
            1.1000,
            1.1008,
        )
        rejected = bar(
            "2026-07-30T08:15:00",
            1.100006,
            1.1010,
            1.1000,
            1.1008,
        )
        self.assertIsNotNone(
            classify_wickless(accepted, tick_size=0.00001, tolerance_ticks=0.5)
        )
        self.assertIsNone(
            classify_wickless(rejected, tick_size=0.00001, tolerance_ticks=0.5)
        )

    def test_doji_and_wrong_side_wicks_are_not_signals(self) -> None:
        doji = bar("2026-07-30T08:00:00", 1.1, 1.101, 1.1, 1.1)
        with_wicks = bar("2026-07-30T08:05:00", 1.1, 1.101, 1.099, 1.1005)
        self.assertIsNone(classify_wickless(doji, tick_size=0.00001))
        self.assertIsNone(classify_wickless(with_wicks, tick_size=0.00001))

    def test_invalid_ohlc_is_rejected(self) -> None:
        invalid = bar("2026-07-30T08:00:00", 1.1, 1.099, 1.098, 1.1005)
        with self.assertRaises(ValueError):
            classify_wickless(invalid, tick_size=0.00001)


class SignalTests(unittest.TestCase):
    def test_buy_signal_has_extreme_stop_and_two_r_target(self) -> None:
        candle = bar("2026-07-30T08:00:00", 1.1000, 1.1010, 1.1000, 1.1008)
        signal = build_signal(candle, StrategyConfig(instrument="eurusd"))
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.side, "BUY")
        self.assertAlmostEqual(signal.stop, 1.0998)
        self.assertAlmostEqual(signal.risk_points, 0.001)
        self.assertAlmostEqual(signal.target, 1.1028)
        self.assertEqual(signal.trigger_level, 1.1)

    def test_sell_signal_has_unique_deterministic_id(self) -> None:
        candle = bar("2026-07-30T08:00:00", 150.0, 150.0, 149.8, 149.9)
        config = StrategyConfig(instrument="usdjpy")
        first = build_signal(candle, config)
        second = build_signal(candle, config)
        self.assertEqual(first, second)
        assert first is not None
        self.assertEqual(len(first.key), 16)
        self.assertEqual(first.side, "SELL")
        self.assertEqual(first.stop, 150.02)
        self.assertEqual(first.target, 149.66)

    def test_only_finalized_fresh_bars_are_returned(self) -> None:
        bars = [
            bar("2026-07-30T08:00:00", 1.1, 1.101, 1.1, 1.1008),
            bar("2026-07-30T08:15:00", 1.1, 1.101, 1.1, 1.1008),
            bar("2026-07-30T08:30:00", 1.1, 1.101, 1.1, 1.1008),
        ]
        signals = find_fresh_signals(
            bars,
            config=StrategyConfig(instrument="eurusd"),
            as_of=datetime(2026, 7, 30, 8, 32, tzinfo=UTC),
            max_signal_age_minutes=30,
        )
        self.assertEqual(
            [item.bar_open_time_utc for item in signals],
            [
                "2026-07-30T08:00:00+00:00",
                "2026-07-30T08:15:00+00:00",
            ],
        )

    def test_discord_embed_is_valid_and_mentions_are_disabled(self) -> None:
        signal = build_signal(
            bar("2026-07-30T08:00:00", 1.1, 1.101, 1.1, 1.1008),
            StrategyConfig(instrument="eurusd"),
        )
        assert signal is not None
        payload = discord_payload(signal)
        self.assertEqual(payload["allowed_mentions"], {"parse": []})
        self.assertIn("BUY EURUSD", payload["embeds"][0]["title"])
        self.assertNotIn(
            "targets the missing",
            payload["embeds"][0]["description"],
        )
        self.assertLess(len(json.dumps(payload)), 6000)


class BacktestTests(unittest.TestCase):
    def test_target_hit_closes_at_two_r(self) -> None:
        bars = [
            bar("2026-07-30T08:00:00", 1.1, 1.101, 1.1, 1.1008),
            bar("2026-07-30T08:15:00", 1.1008, 1.1030, 1.1005, 1.1029),
        ]
        result = run_backtest(
            bars,
            config=StrategyConfig(
                instrument="eurusd",
                slippage_ticks=0,
                commission_per_side=0,
            ),
        )
        self.assertEqual(len(result.signals), 1)
        self.assertEqual(len(result.trades), 1)
        self.assertEqual(result.trades[0].exit_reason, "TARGET")
        self.assertAlmostEqual(result.trades[0].realized_r, 2)

    def test_same_bar_stop_and_target_is_counted_as_stop(self) -> None:
        bars = [
            bar("2026-07-30T08:00:00", 1.1, 1.101, 1.1, 1.1008),
            bar("2026-07-30T08:15:00", 1.1008, 1.1030, 1.0995, 1.1000),
        ]
        result = run_backtest(
            bars,
            config=StrategyConfig(
                instrument="eurusd",
                slippage_ticks=0,
                commission_per_side=0,
            ),
        )
        self.assertEqual(result.trades[0].exit_reason, "STOP_AMBIGUOUS")
        self.assertEqual(result.ambiguous_exits, 1)

    def test_loader_deduplicates_and_drops_zero_range_bars(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bars.csv"
            path.write_text(
                "timestamp,open,high,low,close\n"
                "2026-07-30T08:00:00Z,1,1,1,1\n"
                "2026-07-30T08:15:00Z,1,1.1,0.9,1.05\n"
                "2026-07-30T08:15:00Z,1,1.2,0.9,1.1\n",
                encoding="utf-8",
            )
            loaded = load_bars(path)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].high, 1.2)


class ScannerTests(unittest.TestCase):
    def test_scanner_posts_each_signal_once_across_runs(self) -> None:
        now = datetime(2026, 7, 30, 8, 16, tzinfo=UTC)
        candle = bar("2026-07-30T08:00:00", 1.1, 1.101, 1.1, 1.1008)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            csv_path = root / "eurusd-m15-bid-live.csv"
            state = root / "state" / "seen.json"
            write_csv(csv_path, [candle])
            with patch("wickless_bot.post_discord") as post:
                first = scan_markets(
                    data_dir=root,
                    instruments=["eurusd"],
                    state_path=state,
                    as_of=now,
                    max_signal_age_minutes=20,
                    state_retention_days=14,
                    webhook_url="https://discord.com/api/webhooks/123/token",
                )
                second = scan_markets(
                    data_dir=root,
                    instruments=["eurusd"],
                    state_path=state,
                    as_of=now,
                    max_signal_age_minutes=20,
                    state_retention_days=14,
                    webhook_url="https://discord.com/api/webhooks/123/token",
                )
            post.assert_called_once()
            self.assertEqual(first, (1, 1))
            self.assertEqual(second, (1, 0))
            saved = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(len(saved), 1)

    def test_dry_run_does_not_require_webhook(self) -> None:
        now = datetime(2026, 7, 30, 8, 16, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_csv(
                root / "eurusd-m15-bid-live.csv",
                [bar("2026-07-30T08:00:00", 1.1, 1.101, 1.1, 1.1008)],
            )
            result = scan_markets(
                data_dir=root,
                instruments=["eurusd"],
                state_path=root / "state.json",
                as_of=now,
                max_signal_age_minutes=20,
                state_retention_days=14,
                webhook_url=None,
                dry_run=True,
            )
        self.assertEqual(result, (1, 1))


class SecurityAndProfileTests(unittest.TestCase):
    def test_webhook_validation(self) -> None:
        valid = "https://discord.com/api/webhooks/123/token"
        self.assertEqual(validate_webhook_url(valid), valid)
        invalid = [
            "http://discord.com/api/webhooks/123/token",
            "https://example.com/api/webhooks/123/token",
            "https://discord.com/api/webhooks/not-a-number/token",
            "https://discord.com/api/webhooks/123/token/extra",
            "https://discord.com/api/webhooks/123/token?wait=true",
        ]
        for candidate in invalid:
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                validate_webhook_url(candidate)

    def test_all_major_profiles_are_configured(self) -> None:
        self.assertEqual(LIVE_INSTRUMENTS, ("xauusd", *FOREX_MAJORS))
        self.assertEqual(set(INSTRUMENTS), set(LIVE_INSTRUMENTS))
        self.assertEqual(INSTRUMENTS["usdjpy"].price_decimals, 3)
        self.assertEqual(INSTRUMENTS["eurusd"].price_decimals, 5)

    def test_webhook_is_never_read_from_command_line(self) -> None:
        self.assertNotIn("DISCORD_WEBHOOK_URL", " ".join(os.sys.argv))


if __name__ == "__main__":
    unittest.main()
