#!/usr/bin/env python3
"""Shared engine for trend-filtered no-wick origin-limit entries.

The live scanner and historical research use this same mechanical engine so
Discord alerts cannot drift away from the backtested rules.

The default research setup uses:

* finalized 15-minute bars;
* bullish ``open == low`` / bearish ``open == high`` signals;
* close versus EMA(50) plus a five-bar EMA slope;
* limit entry at the signal candle's opening price;
* a confirmed 3-left / 3-right local pivot stop plus one tick;
* pair, ATR, spread, and execution-cost stop-distance validation;
* a 2R target;
* signals from 09:30 through 13:30 America/New_York;
* multiple pending setups, but at most one active position per pair;
* pending orders cancelled when the trend changes.

Only information available at each historical bar close is used.  Same-bar
fill/exit ambiguity is resolved against the strategy by counting the stop.
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
from dataclasses import asdict, dataclass, replace
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Sequence
from zoneinfo import ZoneInfo

from wickless_bot import (
    DEFAULT_ATR_PERIOD,
    DEFAULT_MAX_COST_TO_RISK_RATIO,
    DEFAULT_MAX_STOP_ATR_FRACTION,
    DEFAULT_MIN_SPREAD_MULTIPLE,
    DEFAULT_MIN_STOP_ATR_FRACTION,
    FOREX_MAJORS,
    INSTRUMENTS,
    Bar,
    StrategyConfig,
    atr_series,
    classify_wickless,
    evaluate_risk_integrity,
    load_bars,
    run_backtest,
)


UTC = timezone.utc
NEW_YORK = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class NoWickConfig:
    instrument: str = "eurusd"
    reward_risk: float = 2.0
    tolerance_ticks: float = 0.5
    trend_filter: str = "ema_slope"
    ema_length: int = 50
    ema_slope_lookback: int = 5
    use_session: bool = True
    session_start: time = time(9, 30)
    session_end: time = time(13, 30)
    stop_mode: str = "confirmed_pivot"
    pivot_left: int = 3
    pivot_right: int = 3
    stop_buffer_ticks: int = 1
    pending_expiry: str = "trend_change"
    expiry_bars: int = 1
    slippage_ticks: float = 1.0
    one_position_per_pair: bool = True
    atr_period: int = DEFAULT_ATR_PERIOD
    minimum_stop_atr_fraction: float = DEFAULT_MIN_STOP_ATR_FRACTION
    maximum_stop_atr_fraction: float = DEFAULT_MAX_STOP_ATR_FRACTION
    minimum_spread_multiple: float = DEFAULT_MIN_SPREAD_MULTIPLE
    maximum_cost_to_risk_ratio: float = DEFAULT_MAX_COST_TO_RISK_RATIO

    def __post_init__(self) -> None:
        if self.instrument not in INSTRUMENTS:
            raise ValueError(f"Unsupported instrument: {self.instrument}")
        if self.reward_risk <= 0:
            raise ValueError("reward_risk must be positive")
        if not 0 <= self.tolerance_ticks <= 2:
            raise ValueError("tolerance_ticks must be between 0 and 2")
        if self.trend_filter not in {"ema_slope", "none"}:
            raise ValueError("trend_filter must be ema_slope or none")
        if self.ema_length < 2 or self.ema_slope_lookback < 1:
            raise ValueError("EMA settings must be positive")
        if self.session_start >= self.session_end:
            raise ValueError("session_start must be before session_end")
        if self.stop_mode not in {"confirmed_pivot", "signal_range"}:
            raise ValueError("stop_mode must be confirmed_pivot or signal_range")
        if self.pivot_left < 1 or self.pivot_right < 1:
            raise ValueError("pivot settings must be positive")
        if self.stop_buffer_ticks < 0:
            raise ValueError("stop_buffer_ticks cannot be negative")
        if self.pending_expiry not in {"trend_change", "bars", "never"}:
            raise ValueError("unsupported pending_expiry")
        if self.expiry_bars < 1:
            raise ValueError("expiry_bars must be positive")
        if self.slippage_ticks < 0:
            raise ValueError("slippage_ticks cannot be negative")
        if self.atr_period < 1:
            raise ValueError("atr_period must be positive")
        if self.minimum_stop_atr_fraction < 0:
            raise ValueError("minimum_stop_atr_fraction cannot be negative")
        if self.maximum_stop_atr_fraction <= 0:
            raise ValueError("maximum_stop_atr_fraction must be positive")
        if self.minimum_spread_multiple < 0:
            raise ValueError("minimum_spread_multiple cannot be negative")
        if self.maximum_cost_to_risk_ratio < 0:
            raise ValueError("maximum_cost_to_risk_ratio cannot be negative")

    @property
    def profile(self):
        return INSTRUMENTS[self.instrument]


@dataclass(frozen=True)
class PendingOrder:
    order_id: str
    instrument: str
    side: str
    pattern: str
    signal_index: int
    signal_time_utc: str
    entry: float
    stop: float
    target: float
    risk: float
    signal_trend: int
    atr_15m: float
    risk_pips: float
    stop_distance_atr: float


@dataclass(frozen=True)
class NoWickTrade:
    order_id: str
    instrument: str
    side: str
    pattern: str
    signal_time_utc: str
    entry_time_utc: str
    exit_time_utc: str
    entry: float
    stop: float
    target: float
    exit: float
    exit_reason: str
    gross_r: float
    net_r_after_costs: float


@dataclass(frozen=True)
class NoWickFill:
    order_id: str
    instrument: str
    side: str
    pattern: str
    signal_time_utc: str
    fill_bar_open_time_utc: str
    fill_time_utc: str
    entry: float
    stop: float
    target: float
    risk: float
    risk_pips: float
    atr_15m: float
    stop_distance_atr: float
    minimum_stop_distance: float
    maximum_stop_distance: float
    minimum_stop_pips: float
    spread: float
    spread_multiple: float
    estimated_round_trip_cost: float
    cost_to_risk_ratio: float
    slippage_ticks_per_side: float


@dataclass
class NoWickResult:
    eligible_signals: int
    pending_orders_created: int
    filled_orders: int
    expired_orders: int
    rejected_no_swing: int
    rejected_invalid_stop: int
    rejected_stop_too_tight: int
    rejected_stop_too_wide: int
    rejected_execution_cost: int
    fills: list[NoWickFill]
    trades: list[NoWickTrade]
    open_positions: int
    pending_at_end: int
    ambiguous_exits: int
    peak_open_positions: int


def ema_series(bars: Sequence[Bar], length: int) -> list[float]:
    if length < 2:
        raise ValueError("length must be at least two")
    if not bars:
        return []
    alpha = 2.0 / (length + 1.0)
    values = [bars[0].close]
    for bar in bars[1:]:
        values.append(alpha * bar.close + (1.0 - alpha) * values[-1])
    return values


def trend_series(
    bars: Sequence[Bar],
    *,
    filter_name: str,
    ema_length: int,
    slope_lookback: int,
) -> list[int]:
    """Return 1 for uptrend, -1 for downtrend, and 0 for neutral/warm-up."""

    if filter_name == "none":
        return [
            1 if bar.close > bar.open else -1 if bar.close < bar.open else 0
            for bar in bars
        ]
    if filter_name != "ema_slope":
        raise ValueError(f"Unsupported trend filter: {filter_name}")
    ema = ema_series(bars, ema_length)
    trends = [0] * len(bars)
    warm_up = max(ema_length - 1, slope_lookback)
    for index in range(warm_up, len(bars)):
        rising = ema[index] > ema[index - slope_lookback]
        falling = ema[index] < ema[index - slope_lookback]
        if bars[index].close > ema[index] and rising:
            trends[index] = 1
        elif bars[index].close < ema[index] and falling:
            trends[index] = -1
    return trends


def confirmed_pivots(
    bars: Sequence[Bar],
    *,
    left: int,
    right: int,
) -> tuple[list[float | None], list[float | None]]:
    """Latest confirmed pivot low/high available at each bar close."""

    latest_low: float | None = None
    latest_high: float | None = None
    lows: list[float | None] = []
    highs: list[float | None] = []
    for confirmation_index in range(len(bars)):
        pivot_index = confirmation_index - right
        if pivot_index >= left:
            window = bars[pivot_index - left : pivot_index + right + 1]
            candidate = bars[pivot_index]
            if candidate.low == min(bar.low for bar in window):
                latest_low = candidate.low
            if candidate.high == max(bar.high for bar in window):
                latest_high = candidate.high
        lows.append(latest_low)
        highs.append(latest_high)
    return lows, highs


def _in_entry_session(bar: Bar, config: NoWickConfig) -> bool:
    if not config.use_session:
        return True
    local_open = bar.timestamp.astimezone(NEW_YORK).time().replace(tzinfo=None)
    return config.session_start <= local_open < config.session_end


def _order_from_signal(
    *,
    bar: Bar,
    index: int,
    trend: int,
    pattern,
    pivot_low: float | None,
    pivot_high: float | None,
    atr_15m: float,
    config: NoWickConfig,
) -> tuple[PendingOrder | None, str | None]:
    entry = bar.open
    if config.stop_mode == "signal_range":
        candle_range = bar.high - bar.low
        stop = (
            entry - candle_range
            if pattern.signal_side == "BUY"
            else entry + candle_range
        )
    else:
        relevant_pivot = pivot_low if pattern.signal_side == "BUY" else pivot_high
        if relevant_pivot is None:
            return None, "NO_SWING"
        buffer_ = config.stop_buffer_ticks * config.profile.tick_size
        stop = (
            relevant_pivot - buffer_
            if pattern.signal_side == "BUY"
            else relevant_pivot + buffer_
        )

    risk = (
        entry - stop
        if pattern.signal_side == "BUY"
        else stop - entry
    )
    if risk <= config.profile.tick_size / 2:
        return None, "INVALID_STOP"
    risk_status, risk_metrics = evaluate_risk_integrity(
        profile=config.profile,
        risk_distance=risk,
        atr_15m=atr_15m,
        spread=0.0,
        min_stop_atr_fraction=config.minimum_stop_atr_fraction,
        max_stop_atr_fraction=config.maximum_stop_atr_fraction,
        min_spread_multiple=config.minimum_spread_multiple,
        max_cost_to_risk_ratio=config.maximum_cost_to_risk_ratio,
        slippage_ticks_per_side=config.slippage_ticks,
    )
    if risk_status is not None:
        return None, risk_status
    target = (
        entry + config.reward_risk * risk
        if pattern.signal_side == "BUY"
        else entry - config.reward_risk * risk
    )
    timestamp = bar.timestamp.astimezone(UTC).isoformat()
    order_id = (
        f"{config.instrument}-{index}-{pattern.kind.lower()}-"
        f"{config.stop_mode}"
    )
    return (
        PendingOrder(
            order_id=order_id,
            instrument=config.instrument,
            side=pattern.signal_side,
            pattern=pattern.kind,
            signal_index=index,
            signal_time_utc=timestamp,
            entry=entry,
            stop=stop,
            target=target,
            risk=risk,
            signal_trend=trend,
            atr_15m=atr_15m,
            risk_pips=risk_metrics["risk_pips"],
            stop_distance_atr=risk_metrics["stop_distance_atr"],
        ),
        None,
    )


def _bar_spread(bid_bar: Bar, ask_bar: Bar) -> float:
    """Conservative observable spread from synchronized OHLC snapshots."""

    return max(
        0.0,
        ask_bar.open - bid_bar.open,
        ask_bar.close - bid_bar.close,
    )


def _limit_touched(order: PendingOrder, bid_bar: Bar, ask_bar: Bar) -> bool:
    return (
        ask_bar.low <= order.entry
        if order.side == "BUY"
        else bid_bar.high >= order.entry
    )


def _exit_flags(
    order: PendingOrder,
    bid_bar: Bar,
    ask_bar: Bar,
) -> tuple[bool, bool]:
    if order.side == "BUY":
        return bid_bar.low <= order.stop, bid_bar.high >= order.target
    return ask_bar.high >= order.stop, ask_bar.low <= order.target


def _close_trade(
    order: PendingOrder,
    *,
    entry_time: datetime,
    exit_time: datetime,
    exit_price: float,
    reason: str,
    config: NoWickConfig,
) -> NoWickTrade:
    direction = 1 if order.side == "BUY" else -1
    gross_r = direction * (exit_price - order.entry) / order.risk
    round_trip_cost = 2 * config.slippage_ticks * config.profile.tick_size
    net_r = gross_r - round_trip_cost / order.risk
    decimals = config.profile.price_decimals
    return NoWickTrade(
        order_id=order.order_id,
        instrument=order.instrument,
        side=order.side,
        pattern=order.pattern,
        signal_time_utc=order.signal_time_utc,
        entry_time_utc=entry_time.astimezone(UTC).isoformat(),
        exit_time_utc=exit_time.astimezone(UTC).isoformat(),
        entry=round(order.entry, decimals),
        stop=round(order.stop, decimals),
        target=round(order.target, decimals),
        exit=round(exit_price, decimals),
        exit_reason=reason,
        gross_r=round(gross_r, 6),
        net_r_after_costs=round(net_r, 6),
    )


def run_no_wick_backtest(
    bars: Sequence[Bar],
    *,
    config: NoWickConfig,
    ask_bars: Sequence[Bar] | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> NoWickResult:
    start = (start or datetime.min.replace(tzinfo=UTC)).astimezone(UTC)
    end = (end or datetime.max.replace(tzinfo=UTC)).astimezone(UTC)
    if start >= end:
        raise ValueError("start must be before end")
    if ask_bars is None:
        ask_bars = bars
    if len(ask_bars) != len(bars) or any(
        bid.timestamp != ask.timestamp
        for bid, ask in zip(bars, ask_bars)
    ):
        raise ValueError("BID and ASK bars must have identical timestamps")

    trends = trend_series(
        bars,
        filter_name=config.trend_filter,
        ema_length=config.ema_length,
        slope_lookback=config.ema_slope_lookback,
    )
    pivot_lows, pivot_highs = confirmed_pivots(
        bars,
        left=config.pivot_left,
        right=config.pivot_right,
    )
    atr_values = atr_series(bars, config.atr_period)
    pending: list[PendingOrder] = []
    positions: list[tuple[PendingOrder, datetime]] = []
    fills: list[NoWickFill] = []
    trades: list[NoWickTrade] = []
    eligible_signals = pending_orders_created = expired_orders = rejected_no_swing = 0
    rejected_invalid_stop = rejected_stop_too_tight = 0
    rejected_stop_too_wide = rejected_execution_cost = 0
    filled_orders = ambiguous_exits = 0
    peak_open_positions = 0

    for index, bar in enumerate(bars):
        ask_bar = ask_bars[index]
        if bar.timestamp < start or bar.timestamp >= end:
            continue

        position_was_open = bool(positions)
        still_open: list[tuple[PendingOrder, datetime]] = []
        for order, entry_time in positions:
            stop_hit, target_hit = _exit_flags(order, bar, ask_bar)
            if not (stop_hit or target_hit):
                still_open.append((order, entry_time))
                continue
            if stop_hit:
                reason = "STOP_AMBIGUOUS" if target_hit else "STOP"
                exit_price = order.stop
                ambiguous_exits += int(target_hit)
            else:
                reason = "TARGET"
                exit_price = order.target
            trades.append(
                _close_trade(
                    order,
                    entry_time=entry_time,
                    exit_time=bar.timestamp,
                    exit_price=exit_price,
                    reason=reason,
                    config=config,
                )
            )
        positions = still_open

        prior_trend = trends[index - 1] if index else 0
        still_pending: list[PendingOrder] = []
        filled_this_bar = False
        for order in pending:
            age = index - order.signal_index
            expired = (
                config.pending_expiry == "trend_change"
                and prior_trend != order.signal_trend
            ) or (
                config.pending_expiry == "bars"
                and age > config.expiry_bars
            )
            if expired:
                expired_orders += 1
                continue
            if config.one_position_per_pair and (
                position_was_open or positions or filled_this_bar
            ):
                still_pending.append(order)
                continue
            if not _limit_touched(order, bar, ask_bar):
                still_pending.append(order)
                continue

            spread = _bar_spread(bar, ask_bar)
            risk_status, risk_metrics = evaluate_risk_integrity(
                profile=config.profile,
                risk_distance=order.risk,
                atr_15m=order.atr_15m,
                spread=spread,
                min_stop_atr_fraction=config.minimum_stop_atr_fraction,
                max_stop_atr_fraction=config.maximum_stop_atr_fraction,
                min_spread_multiple=config.minimum_spread_multiple,
                max_cost_to_risk_ratio=config.maximum_cost_to_risk_ratio,
                slippage_ticks_per_side=config.slippage_ticks,
            )
            if risk_status == "STOP_TOO_TIGHT":
                rejected_stop_too_tight += 1
                continue
            if risk_status == "STOP_TOO_WIDE":
                rejected_stop_too_wide += 1
                continue
            if risk_status is not None:
                rejected_execution_cost += 1
                continue

            filled_orders += 1
            filled_this_bar = True
            fills.append(
                NoWickFill(
                    order_id=order.order_id,
                    instrument=order.instrument,
                    side=order.side,
                    pattern=order.pattern,
                    signal_time_utc=order.signal_time_utc,
                    fill_bar_open_time_utc=bar.timestamp.astimezone(UTC).isoformat(),
                    fill_time_utc=bar.close_time.astimezone(UTC).isoformat(),
                    entry=order.entry,
                    stop=order.stop,
                    target=order.target,
                    risk=order.risk,
                    risk_pips=risk_metrics["risk_pips"],
                    atr_15m=risk_metrics["atr_15m"],
                    stop_distance_atr=risk_metrics["stop_distance_atr"],
                    minimum_stop_distance=risk_metrics["minimum_stop_distance"],
                    maximum_stop_distance=risk_metrics["maximum_stop_distance"],
                    minimum_stop_pips=risk_metrics["minimum_stop_pips"],
                    spread=spread,
                    spread_multiple=risk_metrics["spread_multiple"],
                    estimated_round_trip_cost=risk_metrics[
                        "estimated_round_trip_cost"
                    ],
                    cost_to_risk_ratio=risk_metrics["cost_to_risk_ratio"],
                    slippage_ticks_per_side=config.slippage_ticks,
                )
            )
            stop_hit, target_touched = _exit_flags(order, bar, ask_bar)
            target_after_fill_is_certain = target_touched and (
                (order.side == "BUY" and bar.close >= order.target)
                or (order.side == "SELL" and ask_bar.close <= order.target)
            )
            if stop_hit or target_after_fill_is_certain:
                if stop_hit:
                    reason = "STOP_ON_FILL_AMBIGUOUS"
                    exit_price = order.stop
                    ambiguous_exits += 1
                else:
                    reason = "TARGET_ON_FILL"
                    exit_price = order.target
                trades.append(
                    _close_trade(
                        order,
                        entry_time=bar.timestamp,
                        exit_time=bar.timestamp,
                        exit_price=exit_price,
                        reason=reason,
                        config=config,
                    )
                )
            else:
                positions.append((order, bar.timestamp))
        pending = still_pending
        peak_open_positions = max(peak_open_positions, len(positions))

        pattern = classify_wickless(
            bar,
            tick_size=config.profile.tick_size,
            tolerance_ticks=config.tolerance_ticks,
        )
        if pattern is None or not _in_entry_session(bar, config):
            continue
        signal_trend = trends[index]
        required_trend = 1 if pattern.signal_side == "BUY" else -1
        if config.trend_filter != "none" and signal_trend != required_trend:
            continue
        if config.trend_filter == "none":
            signal_trend = required_trend
        eligible_signals += 1
        order, rejection = _order_from_signal(
            bar=bar,
            index=index,
            trend=signal_trend,
            pattern=pattern,
            pivot_low=pivot_lows[index],
            pivot_high=pivot_highs[index],
            atr_15m=atr_values[index],
            config=config,
        )
        if rejection == "NO_SWING":
            rejected_no_swing += 1
        elif rejection == "INVALID_STOP":
            rejected_invalid_stop += 1
        elif rejection == "STOP_TOO_TIGHT":
            rejected_stop_too_tight += 1
        elif rejection == "STOP_TOO_WIDE":
            rejected_stop_too_wide += 1
        elif rejection is not None:
            rejected_execution_cost += 1
        elif order is not None:
            pending.append(order)
            pending_orders_created += 1

    return NoWickResult(
        eligible_signals=eligible_signals,
        pending_orders_created=pending_orders_created,
        filled_orders=filled_orders,
        expired_orders=expired_orders,
        rejected_no_swing=rejected_no_swing,
        rejected_invalid_stop=rejected_invalid_stop,
        rejected_stop_too_tight=rejected_stop_too_tight,
        rejected_stop_too_wide=rejected_stop_too_wide,
        rejected_execution_cost=rejected_execution_cost,
        fills=fills,
        trades=trades,
        open_positions=len(positions),
        pending_at_end=len(pending),
        ambiguous_exits=ambiguous_exits,
        peak_open_positions=peak_open_positions,
    )


def summarize_result(result: NoWickResult) -> dict[str, float | int | None]:
    values = [trade.net_r_after_costs for trade in result.trades]
    winners = [value for value in values if value > 0]
    losers = [value for value in values if value < 0]
    profit_factor = sum(winners) / -sum(losers) if losers else None
    equity = peak = max_drawdown = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    return {
        "eligible_signals": result.eligible_signals,
        "pending_orders": result.pending_orders_created,
        "fills": result.filled_orders,
        "fill_rate_percent": (
            round(100 * result.filled_orders / result.pending_orders_created, 2)
            if result.pending_orders_created
            else None
        ),
        "closed_trades": len(values),
        "wins": len(winners),
        "losses": len(losers),
        "win_rate_percent": (
            round(100 * len(winners) / len(values), 2) if values else None
        ),
        "net_r_after_costs": round(sum(values), 4),
        "average_net_r": round(statistics.mean(values), 4) if values else None,
        "profit_factor": round(profit_factor, 4) if profit_factor is not None else None,
        "max_drawdown_r": round(max_drawdown, 4),
        "expired_orders": result.expired_orders,
        "rejected_no_swing": result.rejected_no_swing,
        "rejected_invalid_stop": result.rejected_invalid_stop,
        "rejected_stop_too_tight": result.rejected_stop_too_tight,
        "rejected_stop_too_wide": result.rejected_stop_too_wide,
        "rejected_execution_cost": result.rejected_execution_cost,
        "open_positions_at_end": result.open_positions,
        "pending_at_end": result.pending_at_end,
        "ambiguous_exits_counted_as_stop": result.ambiguous_exits,
        "peak_open_positions": result.peak_open_positions,
    }


def _summarize_baseline(
    bars: Sequence[Bar],
    *,
    instrument: str,
    start: datetime,
    end: datetime,
) -> dict[str, float | int | None]:
    config = StrategyConfig(instrument=instrument, reward_risk=2.0)
    result = run_backtest(bars, config=config, start=start, end=end)
    signal_by_key = {signal.key: signal for signal in result.signals}
    net_r = [
        trade.net_points_after_costs / signal_by_key[trade.signal_key].risk_points
        for trade in result.trades
    ]
    winners = [value for value in net_r if value > 0]
    losers = [value for value in net_r if value < 0]
    profit_factor = sum(winners) / -sum(losers) if losers else None
    equity = peak = max_drawdown = 0.0
    for value in net_r:
        equity += value
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    return {
        "eligible_signals": len(result.signals),
        "pending_orders": 0,
        "fills": len(result.signals),
        "fill_rate_percent": 100.0 if result.signals else None,
        "closed_trades": len(net_r),
        "wins": len(winners),
        "losses": len(losers),
        "win_rate_percent": (
            round(100 * len(winners) / len(net_r), 2) if net_r else None
        ),
        "net_r_after_costs": round(sum(net_r), 4),
        "average_net_r": round(statistics.mean(net_r), 4) if net_r else None,
        "profit_factor": round(profit_factor, 4) if profit_factor is not None else None,
        "max_drawdown_r": round(max_drawdown, 4),
        "expired_orders": 0,
        "rejected_no_swing": 0,
        "rejected_invalid_stop": 0,
        "rejected_stop_too_tight": 0,
        "rejected_stop_too_wide": 0,
        "rejected_execution_cost": 0,
        "open_positions_at_end": int(result.open_signal is not None),
        "pending_at_end": 0,
        "ambiguous_exits_counted_as_stop": result.ambiguous_exits,
        "peak_open_positions": int(bool(result.trades or result.open_signal)),
    }


def comparison_variants(instrument: str) -> dict[str, NoWickConfig]:
    base = NoWickConfig(instrument=instrument)
    return {
        "origin_limit": replace(
            base,
            trend_filter="none",
            use_session=False,
            stop_mode="signal_range",
            pending_expiry="bars",
            expiry_bars=1,
        ),
        "ema_range_all_day": replace(
            base,
            use_session=False,
            stop_mode="signal_range",
        ),
        "ema_range_ny": replace(base, stop_mode="signal_range"),
        "ema_pivot_all_day": replace(base, use_session=False),
        "recommended_ema_pivot_ny": base,
    }


def compare_directory(
    *,
    data_dir: Path,
    start: datetime,
    end: datetime,
) -> dict[str, object]:
    per_pair: dict[str, dict[str, object]] = {}
    aggregate: dict[str, dict[str, float | int | None]] = {}
    recommended_trades: list[dict[str, object]] = []
    for instrument in FOREX_MAJORS:
        matches = sorted(data_dir.glob(f"{instrument}-m15-bid-*.csv"))
        if len(matches) != 1:
            raise ValueError(
                f"Expected exactly one {instrument} m15 CSV in {data_dir}, "
                f"found {len(matches)}"
            )
        bars = load_bars(matches[0])
        pair_results: dict[str, object] = {
            "data_file": matches[0].name,
            "bars": sum(start <= bar.timestamp < end for bar in bars),
            "baseline_close_entry": _summarize_baseline(
                bars,
                instrument=instrument,
                start=start,
                end=end,
            ),
        }
        for name, config in comparison_variants(instrument).items():
            result = run_no_wick_backtest(
                bars,
                config=config,
                start=start,
                end=end,
            )
            pair_results[name] = summarize_result(result)
            if name == "recommended_ema_pivot_ny":
                recommended_trades.extend(
                    {
                        "pair": instrument.upper(),
                        **asdict(trade),
                    }
                    for trade in result.trades
                )
        per_pair[instrument] = pair_results

    variant_names = [
        "baseline_close_entry",
        *comparison_variants(FOREX_MAJORS[0]).keys(),
    ]
    for variant in variant_names:
        rows = [per_pair[pair][variant] for pair in FOREX_MAJORS]
        total_trades = sum(int(row["closed_trades"]) for row in rows)
        total_wins = sum(int(row["wins"]) for row in rows)
        total_losses = sum(int(row["losses"]) for row in rows)
        net_r = sum(float(row["net_r_after_costs"]) for row in rows)
        aggregate[variant] = {
            "closed_trades": total_trades,
            "wins": total_wins,
            "losses": total_losses,
            "win_rate_percent": (
                round(100 * total_wins / total_trades, 2)
                if total_trades
                else None
            ),
            "net_r_after_costs": round(net_r, 4),
            "average_net_r": round(net_r / total_trades, 4) if total_trades else None,
            "sum_pair_max_drawdown_r": round(
                sum(float(row["max_drawdown_r"]) for row in rows),
                4,
            ),
            "fills": sum(int(row["fills"]) for row in rows),
            "pending_orders": sum(int(row["pending_orders"]) for row in rows),
            "expired_orders": sum(int(row["expired_orders"]) for row in rows),
            "rejected_stop_too_tight": sum(
                int(row["rejected_stop_too_tight"]) for row in rows
            ),
            "rejected_stop_too_wide": sum(
                int(row["rejected_stop_too_wide"]) for row in rows
            ),
            "rejected_execution_cost": sum(
                int(row["rejected_execution_cost"]) for row in rows
            ),
            "open_positions_at_end": sum(
                int(row["open_positions_at_end"]) for row in rows
            ),
            "pending_at_end": sum(int(row["pending_at_end"]) for row in rows),
            "ambiguous_exits_counted_as_stop": sum(
                int(row["ambiguous_exits_counted_as_stop"]) for row in rows
            ),
            "max_pair_peak_open_positions": max(
                int(row["peak_open_positions"]) for row in rows
            ),
        }

    return {
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "window": {
            "start_utc": start.isoformat(),
            "end_utc_exclusive": end.isoformat(),
        },
        "markets": list(FOREX_MAJORS),
        "timeframe": "15m",
        "reward_risk": 2.0,
        "data": "Dukascopy bid OHLC",
        "cost_model": "one tick of slippage per side; no commission or full spread",
        "execution": {
            "pending_orders": "multiple allowed",
            "trend_expiry": "checked using the prior finalized bar",
            "same_bar_ambiguity": "stop first",
            "fill_bar_target": (
                "credited only when the closing price proves the target was "
                "reached after the limit fill; otherwise deferred"
            ),
            "session_timezone": "America/New_York",
            "risk_integrity": {
                "atr_period": DEFAULT_ATR_PERIOD,
                "minimum_stop_atr_fraction": DEFAULT_MIN_STOP_ATR_FRACTION,
                "maximum_stop_atr_fraction": DEFAULT_MAX_STOP_ATR_FRACTION,
                "minimum_spread_multiple": DEFAULT_MIN_SPREAD_MULTIPLE,
                "maximum_cost_to_risk_ratio": DEFAULT_MAX_COST_TO_RISK_RATIO,
                "pair_minimum_stop": "5 pips FX; 50 XAU pips ($0.50)",
            },
        },
        "variants": {
            "baseline_close_entry": (
                "Current bot: immediate close entry, signal-candle extreme plus "
                "20-tick stop buffer, one position per pair."
            ),
            "origin_limit": (
                "Origin limit, one-candle-range stop, no trend/session filter, "
                "next-bar expiry."
            ),
            "ema_range_all_day": (
                "EMA(50)+slope(5), origin limit, range stop, all day, "
                "trend-change expiry."
            ),
            "ema_range_ny": (
                "EMA/range variant restricted to 09:30–13:30 New York."
            ),
            "ema_pivot_all_day": (
                "EMA(50)+slope(5), origin limit, confirmed pivot(3,3)+one-tick "
                "stop, all day."
            ),
            "recommended_ema_pivot_ny": (
                "Full described defaults adapted to 15m and 2R."
            ),
        },
        "aggregate": aggregate,
        "per_pair": per_pair,
        "recommended_trades": recommended_trades,
        "limitations": [
            "One week is a small sample and cannot substantiate an optimization claim.",
            "Bid-only OHLC omits the live ask spread.",
            "Fifteen-minute OHLC cannot reveal intrabar fill/exit order.",
            "Summed R assumes one unit of risk per filled order; the described "
            "multiple-order mode can create overlapping exposure.",
            "The strategy description was implemented mechanically; protected "
            "third-party source code was not accessed.",
        ],
    }


def write_comparison(output_dir: Path, report: dict[str, object]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "comparison.json").write_text(
        json.dumps(report, indent=2) + "\n",
        encoding="utf-8",
    )
    with (output_dir / "aggregate.csv").open(
        "w",
        newline="",
        encoding="utf-8",
    ) as handle:
        rows = [
            {"variant": name, **values}
            for name, values in report["aggregate"].items()
        ]
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    with (output_dir / "per_pair.csv").open(
        "w",
        newline="",
        encoding="utf-8",
    ) as handle:
        rows = []
        for pair, pair_data in report["per_pair"].items():
            for variant in report["aggregate"]:
                rows.append(
                    {
                        "pair": pair.upper(),
                        "variant": variant,
                        **pair_data[variant],
                    }
                )
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    with (output_dir / "recommended_trades.csv").open(
        "w",
        newline="",
        encoding="utf-8",
    ) as handle:
        rows = report["recommended_trades"]
        if rows:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compare No Wick retracement variants across FX majors."
    )
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--start", required=True, type=_parse_utc)
    parser.add_argument("--end", required=True, type=_parse_utc)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    report = compare_directory(
        data_dir=args.data_dir,
        start=args.start,
        end=args.end,
    )
    write_comparison(args.output, report)
    print(json.dumps(report["aggregate"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
