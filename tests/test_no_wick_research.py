from __future__ import annotations

import unittest
from datetime import datetime, time, timezone

from no_wick_research import (
    NoWickConfig,
    confirmed_pivots,
    ema_series,
    run_no_wick_backtest,
    trend_series,
)
from wickless_bot import Bar


UTC = timezone.utc


def bar(
    minute: int,
    open_: float,
    high: float,
    low: float,
    close: float,
) -> Bar:
    return Bar(
        timestamp=datetime(2026, 7, 27, 13, minute, tzinfo=UTC),
        open=open_,
        high=high,
        low=low,
        close=close,
    )


class IndicatorStateTests(unittest.TestCase):
    def test_ema_uses_only_present_and_prior_closes(self) -> None:
        bars = [
            bar(0, 1.0, 1.1, 0.9, 1.0),
            bar(15, 1.0, 2.1, 0.9, 2.0),
            bar(30, 2.0, 3.1, 1.9, 3.0),
        ]
        self.assertEqual(ema_series(bars, 3), [1.0, 1.5, 2.25])

    def test_price_above_rising_ema_is_uptrend(self) -> None:
        bars = [
            bar(0, 1.00, 1.01, 0.99, 1.00),
            bar(15, 1.00, 1.11, 1.00, 1.10),
            bar(30, 1.10, 1.21, 1.10, 1.20),
            bar(45, 1.20, 1.31, 1.20, 1.30),
        ]
        trends = trend_series(
            bars,
            filter_name="ema_slope",
            ema_length=2,
            slope_lookback=1,
        )
        self.assertEqual(trends[-1], 1)

    def test_pivot_is_unavailable_until_right_bars_confirm_it(self) -> None:
        bars = [
            bar(0, 1.1, 1.2, 1.0, 1.1),
            bar(15, 1.1, 1.2, 0.9, 1.0),
            bar(30, 1.0, 1.1, 1.0, 1.05),
        ]
        lows, _ = confirmed_pivots(bars, left=1, right=1)
        self.assertEqual(lows[:2], [None, None])
        self.assertEqual(lows[2], 0.9)


class NoWickExecutionTests(unittest.TestCase):
    def test_limit_order_waits_for_a_later_bar_and_hits_two_r(self) -> None:
        bars = [
            bar(0, 1.1000, 1.1010, 1.1000, 1.1008),
            bar(15, 1.1008, 1.1010, 1.1000, 1.1007),
            bar(30, 1.1007, 1.1021, 1.1004, 1.1020),
        ]
        config = NoWickConfig(
            instrument="eurusd",
            trend_filter="none",
            use_session=False,
            stop_mode="signal_range",
            pending_expiry="bars",
            expiry_bars=2,
            slippage_ticks=0,
        )
        result = run_no_wick_backtest(bars, config=config)
        self.assertEqual(result.filled_orders, 1)
        self.assertEqual(len(result.fills), 1)
        self.assertEqual(result.fills[0].entry, 1.1)
        self.assertAlmostEqual(result.fills[0].risk_pips, 10.0)
        self.assertAlmostEqual(result.fills[0].stop_distance_atr, 1.0)
        self.assertEqual(result.fills[0].cost_to_risk_ratio, 0.0)
        self.assertEqual(
            result.fills[0].fill_time_utc,
            "2026-07-27T13:30:00+00:00",
        )
        self.assertEqual(len(result.trades), 1)
        self.assertEqual(result.trades[0].exit_reason, "TARGET")
        self.assertEqual(result.trades[0].gross_r, 2)

    def test_unfilled_next_bar_order_expires(self) -> None:
        bars = [
            bar(0, 1.1000, 1.1010, 1.1000, 1.1008),
            bar(15, 1.1008, 1.1020, 1.1005, 1.1015),
            bar(30, 1.1015, 1.1025, 1.1010, 1.1020),
        ]
        config = NoWickConfig(
            instrument="eurusd",
            trend_filter="none",
            use_session=False,
            stop_mode="signal_range",
            pending_expiry="bars",
            expiry_bars=1,
        )
        result = run_no_wick_backtest(bars, config=config)
        self.assertEqual(result.filled_orders, 0)
        self.assertEqual(result.expired_orders, 1)

    def test_same_bar_fill_and_stop_is_conservatively_a_loss(self) -> None:
        bars = [
            bar(0, 1.1000, 1.1010, 1.1000, 1.1008),
            bar(15, 1.1008, 1.1030, 1.0980, 1.1020),
        ]
        config = NoWickConfig(
            instrument="eurusd",
            trend_filter="none",
            use_session=False,
            stop_mode="signal_range",
            pending_expiry="bars",
            slippage_ticks=0,
        )
        result = run_no_wick_backtest(bars, config=config)
        self.assertEqual(result.trades[0].exit_reason, "STOP_ON_FILL_AMBIGUOUS")
        self.assertEqual(result.trades[0].gross_r, -1)
        self.assertEqual(result.ambiguous_exits, 1)

    def test_fill_bar_target_before_unknown_fill_is_not_credited(self) -> None:
        bars = [
            bar(0, 1.1000, 1.1010, 1.1000, 1.1008),
            bar(15, 1.1008, 1.1021, 1.1000, 1.1005),
        ]
        config = NoWickConfig(
            instrument="eurusd",
            trend_filter="none",
            use_session=False,
            stop_mode="signal_range",
            pending_expiry="bars",
            slippage_ticks=0,
        )
        result = run_no_wick_backtest(bars, config=config)
        self.assertEqual(result.filled_orders, 1)
        self.assertEqual(len(result.trades), 0)
        self.assertEqual(result.open_positions, 1)

    def test_new_york_session_uses_signal_bar_open_time(self) -> None:
        inside = bar(30, 1.1000, 1.1010, 1.1000, 1.1008)
        outside = Bar(
            timestamp=datetime(2026, 7, 27, 18, 0, tzinfo=UTC),
            open=1.1000,
            high=1.1010,
            low=1.1000,
            close=1.1008,
        )
        config = NoWickConfig(
            instrument="eurusd",
            trend_filter="none",
            stop_mode="signal_range",
            pending_expiry="never",
            session_start=time(9, 30),
            session_end=time(13, 30),
        )
        result = run_no_wick_backtest([inside, outside], config=config)
        self.assertEqual(result.eligible_signals, 1)

    def test_only_one_position_per_pair_can_be_active(self) -> None:
        bars = [
            bar(0, 1.1000, 1.1010, 1.1000, 1.1008),
            bar(15, 1.1012, 1.1020, 1.1012, 1.1018),
            bar(30, 1.1018, 1.1019, 1.0998, 1.1005),
        ]
        config = NoWickConfig(
            instrument="eurusd",
            trend_filter="none",
            use_session=False,
            stop_mode="signal_range",
            pending_expiry="never",
            slippage_ticks=0,
        )
        result = run_no_wick_backtest(bars, config=config)
        self.assertEqual(result.filled_orders, 1)
        self.assertEqual(result.open_positions, 1)
        self.assertEqual(result.peak_open_positions, 1)
        self.assertGreaterEqual(result.pending_at_end, 1)

    def test_buy_limit_requires_ask_touch_and_long_exit_uses_bid(self) -> None:
        bids = [
            bar(0, 1.1000, 1.1010, 1.1000, 1.1008),
            bar(15, 1.1008, 1.1009, 1.1000, 1.1005),
        ]
        asks = [
            bar(0, 1.1001, 1.1011, 1.1001, 1.1009),
            bar(15, 1.1009, 1.1010, 1.1001, 1.1006),
        ]
        config = NoWickConfig(
            instrument="eurusd",
            trend_filter="none",
            use_session=False,
            stop_mode="signal_range",
            pending_expiry="bars",
        )
        result = run_no_wick_backtest(bids, ask_bars=asks, config=config)
        self.assertEqual(result.filled_orders, 0)

    def test_short_stop_uses_ask_not_bid(self) -> None:
        bids = [
            bar(0, 1.1000, 1.1000, 1.0990, 1.0992),
            bar(15, 1.0992, 1.1000, 1.0988, 1.0995),
            bar(30, 1.0995, 1.1009, 1.0990, 1.1005),
        ]
        asks = [
            bar(0, 1.1001, 1.1001, 1.0991, 1.0993),
            bar(15, 1.0993, 1.1001, 1.0989, 1.0996),
            bar(30, 1.0996, 1.1011, 1.0991, 1.1006),
        ]
        config = NoWickConfig(
            instrument="eurusd",
            trend_filter="none",
            use_session=False,
            stop_mode="signal_range",
            pending_expiry="never",
            slippage_ticks=0,
        )
        result = run_no_wick_backtest(bids, ask_bars=asks, config=config)
        self.assertEqual(result.filled_orders, 1)
        self.assertEqual(result.trades[0].exit_reason, "STOP")

    def test_fill_is_rejected_when_execution_cost_exceeds_ten_percent_of_r(self) -> None:
        bids = [
            bar(0, 1.1000, 1.1010, 1.1000, 1.1008),
            bar(15, 1.1008, 1.1010, 1.1000, 1.1005),
        ]
        asks = [
            bar(0, 1.1002, 1.1012, 1.1002, 1.1010),
            bar(15, 1.1010, 1.1012, 1.1000, 1.1007),
        ]
        config = NoWickConfig(
            instrument="eurusd",
            trend_filter="none",
            use_session=False,
            stop_mode="signal_range",
            pending_expiry="bars",
            slippage_ticks=0,
        )
        result = run_no_wick_backtest(bids, ask_bars=asks, config=config)
        self.assertEqual(result.filled_orders, 0)
        self.assertEqual(result.rejected_execution_cost, 1)


if __name__ == "__main__":
    unittest.main()
