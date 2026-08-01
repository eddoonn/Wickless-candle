from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from wickless_bot import (
    Bar,
    FOREX_MAJORS,
    INSTRUMENTS,
    LIVE_INSTRUMENTS,
    OriginLimitSignal,
    StrategyConfig,
    build_signal,
    classify_wickless,
    discord_payload,
    find_fresh_origin_limit_signals,
    find_fresh_signals,
    find_retrace_signals,
    load_bars,
    retrace_touches_origin,
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


def origin_signal() -> OriginLimitSignal:
    return OriginLimitSignal(
        key="1234567890abcdef",
        instrument="eurusd",
        symbol="EURUSD",
        timeframe="15m",
        pattern="BULLISH_WICKLESS",
        missing_wick="LOWER",
        side="BUY",
        signal_bar_open_time_utc="2026-07-30T13:30:00+00:00",
        signal_time_utc="2026-07-30T13:45:00+00:00",
        fill_bar_open_time_utc="2026-07-30T14:00:00+00:00",
        fill_time_utc="2026-07-30T14:15:00+00:00",
        fill_time_london="2026-07-30T15:15:00+01:00",
        entry_reference=1.1,
        stop=1.099,
        target=1.102,
        risk_points=0.001,
        reward_risk=2,
        ema_length=50,
        ema_slope_lookback=5,
        pivot_left=3,
        pivot_right=3,
        session_label="09:30–13:30 New York",
    )


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
            retrace_bar=bar(
                "2026-07-30T13:00:00",
                1.40447,
                1.40460,
                1.40415,
                1.40430,
            ),
            retrace_bar_number=1,
        )
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.pattern, "BULLISH_WICKLESS")
        self.assertEqual(signal.side, "BUY")
        self.assertEqual(signal.entry_reference, 1.40430)
        self.assertEqual(signal.stop, 1.40395)
        self.assertEqual(signal.target, 1.40500)
        self.assertEqual(signal.retrace_bar_number, 1)
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
        retrace = bar("2026-07-30T08:15:00", 1.1008, 1.1009, 1.1000, 1.1004)
        signal = build_signal(
            candle,
            StrategyConfig(instrument="eurusd"),
            retrace_bar=retrace,
            retrace_bar_number=1,
        )
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.side, "BUY")
        self.assertAlmostEqual(signal.stop, 1.0998)
        self.assertAlmostEqual(signal.risk_points, 0.0006)
        self.assertAlmostEqual(signal.target, 1.1016)
        self.assertEqual(signal.trigger_level, 1.1)
        self.assertEqual(signal.retrace_bar_open_time_utc, "2026-07-30T08:15:00+00:00")
        self.assertEqual(signal.signal_time_utc, "2026-07-30T08:30:00+00:00")

    def test_sell_signal_has_unique_deterministic_id(self) -> None:
        candle = bar("2026-07-30T08:00:00", 150.0, 150.0, 149.8, 149.9)
        retrace = bar("2026-07-30T08:15:00", 149.9, 150.0, 149.7, 149.85)
        config = StrategyConfig(instrument="usdjpy")
        first = build_signal(
            candle,
            config,
            retrace_bar=retrace,
            retrace_bar_number=1,
        )
        second = build_signal(
            candle,
            config,
            retrace_bar=retrace,
            retrace_bar_number=1,
        )
        self.assertEqual(first, second)
        assert first is not None
        self.assertEqual(len(first.key), 16)
        self.assertEqual(first.side, "SELL")
        self.assertEqual(first.stop, 150.02)
        self.assertEqual(first.target, 149.51)

    def test_retrace_margin_versions_the_signal_id(self) -> None:
        candle = bar("2026-07-30T08:00:00", 1.1, 1.101, 1.1, 1.1008)
        retrace = bar("2026-07-30T08:15:00", 1.1008, 1.101, 1.1, 1.1004)
        half_percent = build_signal(
            candle,
            StrategyConfig(instrument="eurusd", retrace_margin_percent=0.5),
            retrace_bar=retrace,
            retrace_bar_number=1,
        )
        quarter_percent = build_signal(
            candle,
            StrategyConfig(instrument="eurusd", retrace_margin_percent=0.25),
            retrace_bar=retrace,
            retrace_bar_number=1,
        )
        assert half_percent is not None
        assert quarter_percent is not None
        self.assertNotEqual(half_percent.key, quarter_percent.key)

    def test_quarter_percent_margin_is_relative_to_the_wickless_open(self) -> None:
        wickless = bar("2026-07-30T08:00:00", 100, 102, 100, 101)
        within = bar("2026-07-30T08:15:00", 102, 102, 100.24, 101.5)
        outside = bar("2026-07-30T08:15:00", 102, 103, 100.26, 102)
        self.assertTrue(
            retrace_touches_origin(wickless, within, margin_percent=0.25)
        )
        self.assertFalse(
            retrace_touches_origin(wickless, outside, margin_percent=0.25)
        )

    def test_default_retrace_margin_is_quarter_percent(self) -> None:
        self.assertEqual(
            StrategyConfig(instrument="eurusd").retrace_margin_percent,
            0.25,
        )

    def test_retrace_must_occur_on_one_of_the_next_three_contiguous_bars(self) -> None:
        bars = [
            bar("2026-07-30T08:00:00", 1.1000, 1.1010, 1.1000, 1.1008),
            bar("2026-07-30T08:15:00", 1.1008, 1.1010, 1.1005, 1.1007),
            bar("2026-07-30T08:30:00", 1.1007, 1.1010, 1.1004, 1.1006),
            bar("2026-07-30T08:45:00", 1.1006, 1.1009, 1.1003, 1.1005),
            bar("2026-07-30T09:00:00", 1.1005, 1.1008, 1.1000, 1.1004),
        ]
        signals = find_retrace_signals(
            bars,
            config=StrategyConfig(
                instrument="eurusd",
                retrace_margin_percent=0,
            ),
        )
        source_signals = [
            item
            for item in signals
            if item.bar_open_time_utc == "2026-07-30T08:00:00+00:00"
        ]
        self.assertEqual(source_signals, [])

    def test_first_qualifying_retrace_is_used_and_later_ones_are_ignored(self) -> None:
        bars = [
            bar("2026-07-30T08:00:00", 1.1000, 1.1010, 1.1000, 1.1008),
            bar("2026-07-30T08:15:00", 1.1008, 1.1010, 1.1005, 1.1007),
            bar("2026-07-30T08:30:00", 1.1007, 1.1010, 1.1000, 1.1005),
            bar("2026-07-30T08:45:00", 1.1005, 1.1008, 1.1000, 1.1004),
        ]
        signals = find_retrace_signals(
            bars,
            config=StrategyConfig(
                instrument="eurusd",
                retrace_margin_percent=0,
            ),
        )
        source_signal = next(
            item
            for item in signals
            if item.bar_open_time_utc == "2026-07-30T08:00:00+00:00"
        )
        self.assertEqual(source_signal.retrace_bar_number, 2)
        self.assertEqual(
            source_signal.retrace_bar_open_time_utc,
            "2026-07-30T08:30:00+00:00",
        )

    def test_third_retrace_bar_is_included(self) -> None:
        bars = [
            bar("2026-07-30T08:00:00", 1.1000, 1.1010, 1.1000, 1.1008),
            bar("2026-07-30T08:15:00", 1.1008, 1.1010, 1.1005, 1.1007),
            bar("2026-07-30T08:30:00", 1.1007, 1.1010, 1.1004, 1.1006),
            bar("2026-07-30T08:45:00", 1.1006, 1.1009, 1.1000, 1.1005),
        ]
        signals = find_retrace_signals(
            bars,
            config=StrategyConfig(
                instrument="eurusd",
                retrace_margin_percent=0,
            ),
        )
        source_signal = next(
            item
            for item in signals
            if item.bar_open_time_utc == "2026-07-30T08:00:00+00:00"
        )
        self.assertEqual(source_signal.retrace_bar_number, 3)

    def test_missing_fifteen_minute_bar_cancels_pending_setup(self) -> None:
        signals = find_retrace_signals(
            [
                bar("2026-07-30T08:00:00", 1.1000, 1.1010, 1.1000, 1.1008),
                bar("2026-07-30T08:30:00", 1.1008, 1.1010, 1.1000, 1.1004),
            ],
            config=StrategyConfig(instrument="eurusd"),
        )
        self.assertEqual(signals, [])

    def test_only_finalized_fresh_bars_are_returned(self) -> None:
        bars = [
            bar("2026-07-30T08:00:00", 1.1, 1.101, 1.1, 1.1008),
            bar("2026-07-30T08:15:00", 1.1008, 1.101, 1.1, 1.1004),
            bar("2026-07-30T08:30:00", 1.1004, 1.101, 1.099, 1.1000),
        ]
        signals = find_fresh_signals(
            bars,
            config=StrategyConfig(instrument="eurusd"),
            as_of=datetime(2026, 7, 30, 8, 31, tzinfo=UTC),
            max_signal_age_minutes=30,
        )
        self.assertEqual(
            [item.bar_open_time_utc for item in signals],
            ["2026-07-30T08:00:00+00:00"],
        )

    def test_discord_embed_is_valid_and_mentions_are_disabled(self) -> None:
        payload = discord_payload(origin_signal())
        self.assertEqual(payload["allowed_mentions"], {"parse": []})
        self.assertIn("BUY EURUSD", payload["embeds"][0]["title"])
        self.assertIn("exact no-wick origin limit", payload["embeds"][0]["description"])
        self.assertIn("EMA 50", json.dumps(payload))
        self.assertNotIn("0.25%", json.dumps(payload))
        self.assertLess(len(json.dumps(payload)), 6000)


class BacktestTests(unittest.TestCase):
    def test_target_hit_closes_at_two_r(self) -> None:
        bars = [
            bar("2026-07-30T08:00:00", 1.1, 1.101, 1.1, 1.1008),
            bar("2026-07-30T08:15:00", 1.1008, 1.1009, 1.1, 1.1004),
            bar("2026-07-30T08:30:00", 1.1004, 1.1020, 1.1002, 1.1019),
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
            bar("2026-07-30T08:15:00", 1.1008, 1.1009, 1.1, 1.1004),
            bar("2026-07-30T08:30:00", 1.1004, 1.1020, 1.0995, 1.1000),
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
    def test_live_scanner_uses_the_verified_strategy_defaults(self) -> None:
        candles = [
            bar("2026-07-30T13:00:00", 1.1, 1.101, 1.099, 1.1005),
        ]
        with patch(
            "no_wick_research.run_no_wick_backtest",
            return_value=SimpleNamespace(fills=[]),
        ) as engine:
            find_fresh_origin_limit_signals(
                candles,
                instrument="eurusd",
                as_of=datetime(2026, 7, 30, 13, 16, tzinfo=UTC),
            )
        config = engine.call_args.kwargs["config"]
        self.assertEqual(config.ema_length, 50)
        self.assertEqual(config.ema_slope_lookback, 5)
        self.assertEqual((config.pivot_left, config.pivot_right), (3, 3))
        self.assertEqual(config.stop_buffer_ticks, 1)
        self.assertEqual(config.reward_risk, 2)
        self.assertEqual(config.pending_expiry, "trend_change")
        self.assertTrue(config.use_session)

    def test_scanner_does_not_alert_on_unconfirmed_no_wick_candle(self) -> None:
        now = datetime(2026, 7, 30, 8, 16, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_csv(
                root / "eurusd-m15-bid-live.csv",
                [bar("2026-07-30T08:00:00", 1.1, 1.101, 1.1, 1.1008)],
            )
            with patch("wickless_bot.post_discord") as post:
                result = scan_markets(
                    data_dir=root,
                    instruments=["eurusd"],
                    state_path=root / "state.json",
                    as_of=now,
                    max_signal_age_minutes=20,
                    state_retention_days=14,
                    webhook_url="https://discord.com/api/webhooks/123/token",
                )
            post.assert_not_called()
        self.assertEqual(result, (0, 0))

    def test_scanner_posts_each_signal_once_across_runs(self) -> None:
        now = datetime(2026, 7, 30, 8, 31, tzinfo=UTC)
        candles = [
            bar("2026-07-30T08:00:00", 1.1, 1.101, 1.1, 1.1008),
            bar("2026-07-30T08:15:00", 1.1008, 1.1009, 1.1, 1.1004),
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            csv_path = root / "eurusd-m15-bid-live.csv"
            state = root / "state" / "seen.json"
            write_csv(csv_path, candles)
            with (
                patch(
                    "wickless_bot.find_fresh_origin_limit_signals",
                    return_value=[origin_signal()],
                ),
                patch("wickless_bot.post_discord") as post,
            ):
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
        now = datetime(2026, 7, 30, 8, 31, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_csv(
                root / "eurusd-m15-bid-live.csv",
                [
                    bar("2026-07-30T08:00:00", 1.1, 1.101, 1.1, 1.1008),
                    bar(
                        "2026-07-30T08:15:00",
                        1.1008,
                        1.1009,
                        1.1,
                        1.1004,
                    ),
                ],
            )
            with patch(
                "wickless_bot.find_fresh_origin_limit_signals",
                return_value=[origin_signal()],
            ):
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
