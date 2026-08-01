#!/usr/bin/env python3
"""Fifteen-minute wickless-candle backtester and Discord signal bot.

The detector is an independent implementation of the public behavior described
for xGhozt Wickless Candles.  It intentionally uses only Python's standard
library; Dukascopy OHLC data is supplied as CSV by ``live_data.py`` or
``dukascopy-node``.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import statistics
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Sequence
from zoneinfo import ZoneInfo


UTC = timezone.utc
LONDON = ZoneInfo("Europe/London")
TIMEFRAME_MINUTES = 15
TIMEFRAME_LABEL = f"{TIMEFRAME_MINUTES}m"
DATA_TIMEFRAME = f"m{TIMEFRAME_MINUTES}"
DEFAULT_MAX_SIGNAL_AGE_SECONDS = 120
DEFAULT_MAX_QUOTE_AGE_SECONDS = 120
DEFAULT_MAX_ENTRY_DEVIATION_R = 0.25
DEFAULT_RESEARCH_LOOKBACK_SECONDS = 45 * 60
DEFAULT_ATR_PERIOD = 14
DEFAULT_MIN_STOP_ATR_FRACTION = 0.40
DEFAULT_MAX_STOP_ATR_FRACTION = 1.50
DEFAULT_MIN_SPREAD_MULTIPLE = 3.0
DEFAULT_MAX_COST_TO_RISK_RATIO = 0.10
DEFAULT_SLIPPAGE_TICKS_PER_SIDE = 1.0

ACTIONABLE = "ACTIONABLE"
EXPIRED_BY_AGE = "EXPIRED_BY_AGE"
STALE_QUOTE = "STALE_QUOTE"
ASK_FILL_NOT_CONFIRMED = "ASK_FILL_NOT_CONFIRMED"
STOP_ALREADY_REACHED = "STOP_ALREADY_REACHED"
TARGET_ALREADY_REACHED = "TARGET_ALREADY_REACHED"
AMBIGUOUS_PRICE_PATH = "AMBIGUOUS_PRICE_PATH"
PRICE_MOVED_TOO_FAR = "PRICE_MOVED_TOO_FAR"
ACTIVE_POSITION_EXISTS = "ACTIVE_POSITION_EXISTS"
DUPLICATE_SIGNAL = "DUPLICATE_SIGNAL"
STOP_TOO_TIGHT = "STOP_TOO_TIGHT"
STOP_TOO_WIDE = "STOP_TOO_WIDE"
EXECUTION_COST_TOO_LARGE_RELATIVE_TO_RISK = (
    "EXECUTION_COST_TOO_LARGE_RELATIVE_TO_RISK"
)


@dataclass(frozen=True)
class InstrumentProfile:
    key: str
    symbol: str
    base_currency: str
    quote_currency: str
    pip_size: float
    tick_size: float
    price_decimals: int
    stop_buffer_ticks: int
    minimum_stop_pips: float
    jetta_code: str

    @property
    def minimum_stop_distance(self) -> float:
        return self.minimum_stop_pips * self.pip_size

    def to_pips(self, distance: float) -> float:
        return distance / self.pip_size


INSTRUMENTS = {
    "xauusd": InstrumentProfile(
        "xauusd", "XAUUSD", "XAU", "USD", 0.01, 0.001, 3, 20, 50.0, "XAU-USD"
    ),
    "eurusd": InstrumentProfile(
        "eurusd", "EURUSD", "EUR", "USD", 0.0001, 0.00001, 5, 20, 5.0, "EUR-USD"
    ),
    "gbpusd": InstrumentProfile(
        "gbpusd", "GBPUSD", "GBP", "USD", 0.0001, 0.00001, 5, 20, 5.0, "GBP-USD"
    ),
    "usdjpy": InstrumentProfile(
        "usdjpy", "USDJPY", "USD", "JPY", 0.01, 0.001, 3, 20, 5.0, "USD-JPY"
    ),
    "usdchf": InstrumentProfile(
        "usdchf", "USDCHF", "USD", "CHF", 0.0001, 0.00001, 5, 20, 5.0, "USD-CHF"
    ),
    "usdcad": InstrumentProfile(
        "usdcad", "USDCAD", "USD", "CAD", 0.0001, 0.00001, 5, 20, 5.0, "USD-CAD"
    ),
    "audusd": InstrumentProfile(
        "audusd", "AUDUSD", "AUD", "USD", 0.0001, 0.00001, 5, 20, 5.0, "AUD-USD"
    ),
    "nzdusd": InstrumentProfile(
        "nzdusd", "NZDUSD", "NZD", "USD", 0.0001, 0.00001, 5, 20, 5.0, "NZD-USD"
    ),
}
FOREX_MAJORS = (
    "eurusd",
    "gbpusd",
    "usdjpy",
    "usdchf",
    "usdcad",
    "audusd",
    "nzdusd",
)
LIVE_INSTRUMENTS = ("xauusd", *FOREX_MAJORS)


@dataclass(frozen=True)
class Bar:
    """A UTC OHLC bar whose timestamp is the bar's opening time."""

    timestamp: datetime
    open: float
    high: float
    low: float
    close: float

    @property
    def close_time(self) -> datetime:
        return self.timestamp + timedelta(minutes=TIMEFRAME_MINUTES)


@dataclass(frozen=True)
class StrategyConfig:
    """Legacy confirmation-close baseline retained for research comparisons."""

    instrument: str = "eurusd"
    reward_risk: float = 2.0
    tolerance_ticks: float = 0.5
    retrace_bars: int = 3
    retrace_margin_percent: float = 0.25
    stop_buffer_ticks: int | None = None
    slippage_ticks: float = 1.0
    commission_per_side: float = 0.0

    def __post_init__(self) -> None:
        if self.instrument not in INSTRUMENTS:
            raise ValueError(f"Unsupported instrument: {self.instrument}")
        if self.reward_risk <= 0:
            raise ValueError("reward_risk must be positive")
        if not 0 <= self.tolerance_ticks <= 2:
            raise ValueError("tolerance_ticks must be between 0 and 2")
        if not 1 <= self.retrace_bars <= 20:
            raise ValueError("retrace_bars must be between 1 and 20")
        if not 0 <= self.retrace_margin_percent <= 100:
            raise ValueError("retrace_margin_percent must be between 0 and 100")
        if self.effective_stop_buffer_ticks < 0:
            raise ValueError("stop_buffer_ticks cannot be negative")
        if self.slippage_ticks < 0 or self.commission_per_side < 0:
            raise ValueError("Costs cannot be negative")

    @property
    def profile(self) -> InstrumentProfile:
        return INSTRUMENTS[self.instrument]

    @property
    def effective_stop_buffer_ticks(self) -> int:
        configured = self.stop_buffer_ticks
        return self.profile.stop_buffer_ticks if configured is None else configured

    @property
    def tolerance(self) -> float:
        return self.profile.tick_size * self.tolerance_ticks

    @property
    def stop_buffer(self) -> float:
        return self.profile.tick_size * self.effective_stop_buffer_ticks


@dataclass(frozen=True)
class WicklessPattern:
    kind: str
    missing_wick: str
    signal_side: str


@dataclass(frozen=True)
class Signal:
    key: str
    instrument: str
    symbol: str
    timeframe: str
    pattern: str
    missing_wick: str
    side: str
    bar_open_time_utc: str
    retrace_bar_open_time_utc: str
    retrace_bar_number: int
    retrace_window_bars: int
    retrace_margin_percent: float
    signal_time_utc: str
    signal_time_london: str
    entry_reference: float
    stop: float
    target: float
    trigger_level: float
    candle_open: float
    candle_high: float
    candle_low: float
    candle_close: float
    risk_points: float
    reward_risk: float


@dataclass(frozen=True)
class OriginLimitSignal:
    """A confirmed entry from the live trend-filtered origin-reclaim strategy."""

    key: str
    instrument: str
    symbol: str
    timeframe: str
    pattern: str
    missing_wick: str
    side: str
    signal_bar_open_time_utc: str
    signal_time_utc: str
    fill_bar_open_time_utc: str
    fill_time_utc: str
    fill_time_london: str
    entry_reference: float
    stop: float
    target: float
    risk_points: float
    reward_risk: float
    ema_length: int
    ema_slope_lookback: int
    pivot_left: int
    pivot_right: int
    session_label: str
    detected_time_utc: str = ""
    published_time_utc: str = ""
    current_bid: float = 0.0
    current_ask: float = 0.0
    current_spread: float = 0.0
    distance_from_entry_points: float = 0.0
    distance_from_entry_r: float = 0.0
    signal_age_seconds: int = 0
    actionability_status: str = "UNVALIDATED"
    risk_pips: float = 0.0
    atr_15m: float = 0.0
    stop_distance_atr: float = 0.0
    minimum_stop_distance: float = 0.0
    maximum_stop_distance: float = 0.0
    minimum_stop_pips: float = 0.0
    spread_multiple: float = 0.0
    estimated_round_trip_cost: float = 0.0
    cost_to_risk_ratio: float = 0.0
    slippage_ticks_per_side: float = DEFAULT_SLIPPAGE_TICKS_PER_SIDE
    # Keep the legacy default so Phase 2 state can be reloaded safely; new
    # signals always set this explicitly from the shared engine configuration.
    entry_model: str = "origin_limit"
    origin_price: float = 0.0
    origin_zone_low: float = 0.0
    origin_zone_high: float = 0.0
    touch_bar_number: int = 0
    confirmation_bar_number: int = 0
    body_ratio: float = 0.0
    wick_size_ticks: float = 0.0
    wickless_range_atr: float = 0.0
    close_location: float = 0.0
    quality_score: float = 0.0
    entry_displacement_atr: float = 0.0


@dataclass(frozen=True)
class CurrentQuote:
    instrument: str
    observed_time_utc: str
    bid: float
    ask: float
    spread: float
    source: str


@dataclass
class ScannerState:
    version: int
    handled: dict[str, dict[str, object]]
    positions: dict[str, dict[str, object]]
    rejections: list[dict[str, object]]


@dataclass(frozen=True)
class Trade:
    signal_key: str
    instrument: str
    side: str
    pattern: str
    entry_time_utc: str
    exit_time_utc: str
    entry: float
    exit: float
    stop: float
    target: float
    exit_reason: str
    realized_r: float
    gross_points: float
    net_points_after_costs: float


@dataclass
class BacktestResult:
    signals: list[Signal]
    trades: list[Trade]
    open_signal: Signal | None
    ambiguous_exits: int = 0


def parse_iso_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def validate_bar(bar: Bar) -> None:
    prices = (bar.open, bar.high, bar.low, bar.close)
    if not all(math.isfinite(price) and price > 0 for price in prices):
        raise ValueError(f"Invalid non-positive or non-finite OHLC bar: {bar}")
    if bar.low > min(bar.open, bar.close) or bar.high < max(bar.open, bar.close):
        raise ValueError(f"Inconsistent OHLC bar: {bar}")


def atr_series(bars: Sequence[Bar], period: int = DEFAULT_ATR_PERIOD) -> list[float]:
    """Return a causal simple ATR, using only bars known at each index."""

    if period < 1:
        raise ValueError("ATR period must be positive")
    true_ranges: list[float] = []
    values: list[float] = []
    previous_close: float | None = None
    for bar in bars:
        validate_bar(bar)
        true_range = bar.high - bar.low
        if previous_close is not None:
            true_range = max(
                true_range,
                abs(bar.high - previous_close),
                abs(bar.low - previous_close),
            )
        true_ranges.append(true_range)
        window = true_ranges[-period:]
        values.append(sum(window) / len(window))
        previous_close = bar.close
    return values


def evaluate_risk_integrity(
    *,
    profile: InstrumentProfile,
    risk_distance: float,
    atr_15m: float,
    spread: float,
    min_stop_atr_fraction: float = DEFAULT_MIN_STOP_ATR_FRACTION,
    max_stop_atr_fraction: float = DEFAULT_MAX_STOP_ATR_FRACTION,
    min_spread_multiple: float = DEFAULT_MIN_SPREAD_MULTIPLE,
    max_cost_to_risk_ratio: float = DEFAULT_MAX_COST_TO_RISK_RATIO,
    slippage_ticks_per_side: float = DEFAULT_SLIPPAGE_TICKS_PER_SIDE,
) -> tuple[str | None, dict[str, float]]:
    """Evaluate the shared pair, volatility, spread, and cost risk rules."""

    values = (
        risk_distance,
        atr_15m,
        spread,
        min_stop_atr_fraction,
        max_stop_atr_fraction,
        min_spread_multiple,
        max_cost_to_risk_ratio,
        slippage_ticks_per_side,
    )
    if not all(math.isfinite(value) for value in values):
        raise ValueError("Risk inputs must be finite")
    if risk_distance <= 0 or atr_15m <= 0:
        raise ValueError("Risk distance and ATR must be positive")
    if spread < 0 or min_stop_atr_fraction < 0 or min_spread_multiple < 0:
        raise ValueError("Spread and risk floors cannot be negative")
    if max_stop_atr_fraction <= 0 or max_cost_to_risk_ratio < 0:
        raise ValueError("Maximum risk settings must be positive")
    if slippage_ticks_per_side < 0:
        raise ValueError("Slippage cannot be negative")

    atr_floor = atr_15m * min_stop_atr_fraction
    spread_floor = spread * min_spread_multiple
    minimum_stop = max(
        profile.minimum_stop_distance,
        atr_floor,
        spread_floor,
    )
    maximum_stop = atr_15m * max_stop_atr_fraction
    estimated_cost = spread + (
        2 * slippage_ticks_per_side * profile.tick_size
    )
    cost_to_risk = estimated_cost / risk_distance
    metrics = {
        "risk_pips": profile.to_pips(risk_distance),
        "atr_15m": atr_15m,
        "stop_distance_atr": risk_distance / atr_15m,
        "minimum_stop_distance": minimum_stop,
        "maximum_stop_distance": maximum_stop,
        "minimum_stop_pips": profile.minimum_stop_pips,
        "spread_multiple": risk_distance / spread if spread else 0.0,
        "estimated_round_trip_cost": estimated_cost,
        "cost_to_risk_ratio": cost_to_risk,
    }
    tolerance = profile.tick_size / 100
    if risk_distance + tolerance < minimum_stop:
        return STOP_TOO_TIGHT, metrics
    if risk_distance - tolerance > maximum_stop:
        return STOP_TOO_WIDE, metrics
    if cost_to_risk > max_cost_to_risk_ratio + 1e-12:
        return EXECUTION_COST_TOO_LARGE_RELATIVE_TO_RISK, metrics
    return None, metrics


def classify_wickless(
    bar: Bar,
    *,
    tick_size: float,
    tolerance_ticks: float = 0.5,
) -> WicklessPattern | None:
    """Classify a directional candle with no wick from its opening price.

    Signal direction follows the candle: bullish ``open == low`` candles
    produce BUY signals, while bearish ``open == high`` candles produce SELL
    signals.
    """

    validate_bar(bar)
    if tick_size <= 0:
        raise ValueError("tick_size must be positive")
    if not 0 <= tolerance_ticks <= 2:
        raise ValueError("tolerance_ticks must be between 0 and 2")
    tolerance = tick_size * tolerance_ticks + 1e-12

    if bar.close > bar.open and bar.open - bar.low <= tolerance:
        return WicklessPattern(
            kind="BULLISH_WICKLESS",
            missing_wick="LOWER",
            signal_side="BUY",
        )
    if bar.close < bar.open and bar.high - bar.open <= tolerance:
        return WicklessPattern(
            kind="BEARISH_WICKLESS",
            missing_wick="UPPER",
            signal_side="SELL",
        )
    return None


def _price(value: float, profile: InstrumentProfile) -> float:
    return round(value, profile.price_decimals)


def retrace_touches_origin(
    wickless_bar: Bar,
    retrace_bar: Bar,
    *,
    margin_percent: float,
) -> bool:
    """Return whether a later bar trades inside the origin-price margin band."""

    validate_bar(wickless_bar)
    validate_bar(retrace_bar)
    if not 0 <= margin_percent <= 100:
        raise ValueError("margin_percent must be between 0 and 100")
    margin = wickless_bar.open * margin_percent / 100
    lower = wickless_bar.open - margin
    upper = wickless_bar.open + margin
    return retrace_bar.low <= upper + 1e-12 and retrace_bar.high >= lower - 1e-12


def build_signal(
    wickless_bar: Bar,
    config: StrategyConfig,
    *,
    retrace_bar: Bar,
    retrace_bar_number: int,
) -> Signal | None:
    pattern = classify_wickless(
        wickless_bar,
        tick_size=config.profile.tick_size,
        tolerance_ticks=config.tolerance_ticks,
    )
    if (
        pattern is None
        or not 1 <= retrace_bar_number <= config.retrace_bars
        or not retrace_touches_origin(
            wickless_bar,
            retrace_bar,
            margin_percent=config.retrace_margin_percent,
        )
    ):
        return None

    entry = retrace_bar.close
    if pattern.signal_side == "SELL":
        stop = wickless_bar.high + config.stop_buffer
        risk = stop - entry
        target = entry - config.reward_risk * risk
    else:
        stop = wickless_bar.low - config.stop_buffer
        risk = entry - stop
        target = entry + config.reward_risk * risk
    if risk <= config.profile.tick_size / 2:
        return None

    signal_time = retrace_bar.close_time.astimezone(UTC)
    identity = (
        f"wickless-v4-retrace|{config.instrument}|"
        f"{wickless_bar.timestamp.isoformat()}|{retrace_bar.timestamp.isoformat()}|"
        f"{pattern.kind}|{TIMEFRAME_LABEL}|{config.retrace_bars}|"
        f"{config.retrace_margin_percent:g}"
    )
    key = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    profile = config.profile
    return Signal(
        key=key,
        instrument=config.instrument,
        symbol=profile.symbol,
        timeframe=TIMEFRAME_LABEL,
        pattern=pattern.kind,
        missing_wick=pattern.missing_wick,
        side=pattern.signal_side,
        bar_open_time_utc=wickless_bar.timestamp.astimezone(UTC).isoformat(),
        retrace_bar_open_time_utc=retrace_bar.timestamp.astimezone(UTC).isoformat(),
        retrace_bar_number=retrace_bar_number,
        retrace_window_bars=config.retrace_bars,
        retrace_margin_percent=config.retrace_margin_percent,
        signal_time_utc=signal_time.isoformat(),
        signal_time_london=signal_time.astimezone(LONDON).isoformat(),
        entry_reference=_price(entry, profile),
        stop=_price(stop, profile),
        target=_price(target, profile),
        trigger_level=_price(wickless_bar.open, profile),
        candle_open=_price(wickless_bar.open, profile),
        candle_high=_price(wickless_bar.high, profile),
        candle_low=_price(wickless_bar.low, profile),
        candle_close=_price(wickless_bar.close, profile),
        risk_points=_price(risk, profile),
        reward_risk=config.reward_risk,
    )


def _parse_timestamp(raw: str) -> datetime:
    value = raw.strip()
    try:
        numeric = float(value)
    except ValueError:
        return parse_iso_datetime(value)
    if numeric > 10_000_000_000:
        numeric /= 1000
    return datetime.fromtimestamp(numeric, UTC)


def load_bars(path: Path) -> list[Bar]:
    """Read, validate, sort, and de-duplicate a Dukascopy-style OHLC CSV."""

    bars_by_time: dict[datetime, Bar] = {}
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        required = {"timestamp", "open", "high", "low", "close"}
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise ValueError(f"{path} must contain columns: {sorted(required)}")
        for line_number, row in enumerate(reader, start=2):
            try:
                bar = Bar(
                    timestamp=_parse_timestamp(row["timestamp"]),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                )
                validate_bar(bar)
            except (TypeError, ValueError) as error:
                raise ValueError(f"{path}:{line_number}: {error}") from error
            if bar.timestamp.minute % TIMEFRAME_MINUTES != 0 or bar.timestamp.second:
                raise ValueError(
                    f"{path}:{line_number}: timestamp is not aligned to "
                    f"{TIMEFRAME_MINUTES} minutes"
                )
            if bar.high == bar.low:
                continue
            bars_by_time[bar.timestamp] = bar
    return [bars_by_time[key] for key in sorted(bars_by_time)]


def find_fresh_origin_limit_signals(
    bars: Sequence[Bar],
    *,
    instrument: str,
    as_of: datetime,
    max_signal_age_seconds: int = DEFAULT_MAX_SIGNAL_AGE_SECONDS,
    ask_bars: Sequence[Bar] | None = None,
) -> list[OriginLimitSignal]:
    """Return recent origin-touch/reclaim entries from the shared strategy engine."""

    if max_signal_age_seconds < 1:
        raise ValueError("max_signal_age_seconds must be positive")
    # Imported lazily to avoid a module cycle: the shared strategy engine uses
    # the detector and Bar type defined above.
    from no_wick_research import NoWickConfig, run_no_wick_backtest

    as_of = as_of.astimezone(UTC)
    finalized = [
        bar
        for bar in sorted(bars, key=lambda item: item.timestamp)
        if bar.close_time <= as_of
    ]
    finalized_ask = None
    if ask_bars is not None:
        finalized_ask = [
            bar
            for bar in sorted(ask_bars, key=lambda item: item.timestamp)
            if bar.close_time <= as_of
        ]
    config = NoWickConfig(instrument=instrument)
    result = run_no_wick_backtest(
        finalized,
        config=config,
        ask_bars=finalized_ask,
    )
    cutoff = as_of - timedelta(seconds=max_signal_age_seconds)
    profile = config.profile
    signals: list[OriginLimitSignal] = []
    for fill in result.fills:
        fill_time = parse_iso_datetime(fill.fill_time_utc)
        if not cutoff <= fill_time <= as_of:
            continue
        signal_bar_open = parse_iso_datetime(fill.signal_time_utc)
        signal_close = signal_bar_open + timedelta(minutes=TIMEFRAME_MINUTES)
        identity = (
            f"no-wick-origin-reclaim-v4-market-side|{instrument}|{fill.signal_time_utc}|"
            f"{fill.fill_bar_open_time_utc}|{fill.pattern}|{TIMEFRAME_LABEL}|"
            f"{config.ema_length}|{config.ema_slope_lookback}|"
            f"{config.pivot_left}|{config.pivot_right}|{config.reward_risk:g}|"
            f"{config.session_start.isoformat()}|{config.session_end.isoformat()}|"
            f"{config.expiry_bars}"
        )
        signals.append(
            OriginLimitSignal(
                key=hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16],
                instrument=instrument,
                symbol=profile.symbol,
                timeframe=TIMEFRAME_LABEL,
                pattern=fill.pattern,
                missing_wick=(
                    "LOWER" if fill.side == "BUY" else "UPPER"
                ),
                side=fill.side,
                signal_bar_open_time_utc=fill.signal_time_utc,
                signal_time_utc=signal_close.astimezone(UTC).isoformat(),
                fill_bar_open_time_utc=fill.fill_bar_open_time_utc,
                fill_time_utc=fill.fill_time_utc,
                fill_time_london=fill_time.astimezone(LONDON).isoformat(),
                entry_reference=_price(fill.entry, profile),
                stop=_price(fill.stop, profile),
                target=_price(fill.target, profile),
                risk_points=_price(fill.risk, profile),
                reward_risk=config.reward_risk,
                ema_length=config.ema_length,
                ema_slope_lookback=config.ema_slope_lookback,
                pivot_left=config.pivot_left,
                pivot_right=config.pivot_right,
                session_label=(
                    f"{config.session_start.strftime('%H:%M')}–"
                    f"{config.session_end.strftime('%H:%M')} New York"
                ),
                risk_pips=round(fill.risk_pips, 2),
                atr_15m=_price(fill.atr_15m, profile),
                stop_distance_atr=round(fill.stop_distance_atr, 4),
                minimum_stop_distance=_price(
                    fill.minimum_stop_distance, profile
                ),
                maximum_stop_distance=_price(
                    fill.maximum_stop_distance, profile
                ),
                minimum_stop_pips=fill.minimum_stop_pips,
                spread_multiple=round(fill.spread_multiple, 4),
                estimated_round_trip_cost=_price(
                    fill.estimated_round_trip_cost, profile
                ),
                cost_to_risk_ratio=round(fill.cost_to_risk_ratio, 4),
                slippage_ticks_per_side=fill.slippage_ticks_per_side,
                entry_model=config.entry_model,
                origin_price=_price(fill.origin_price, profile),
                origin_zone_low=_price(fill.origin_zone_low, profile),
                origin_zone_high=_price(fill.origin_zone_high, profile),
                touch_bar_number=fill.touch_bar_number,
                confirmation_bar_number=fill.confirmation_bar_number,
                body_ratio=round(fill.body_ratio, 4),
                wick_size_ticks=round(fill.wick_size_ticks, 4),
                wickless_range_atr=round(fill.wickless_range_atr, 4),
                close_location=round(fill.close_location, 4),
                quality_score=round(fill.quality_score, 2),
                entry_displacement_atr=round(fill.entry_displacement_atr, 4),
            )
        )
    return signals


def find_fresh_signals(
    bars: Sequence[Bar],
    *,
    config: StrategyConfig,
    as_of: datetime,
    max_signal_age_minutes: int = 45,
) -> list[Signal]:
    as_of = as_of.astimezone(UTC)
    if max_signal_age_minutes < TIMEFRAME_MINUTES:
        raise ValueError(
            f"max_signal_age_minutes must be at least {TIMEFRAME_MINUTES}"
        )
    cutoff = as_of - timedelta(minutes=max_signal_age_minutes)
    return [
        signal
        for signal in find_retrace_signals(bars, config=config)
        if parse_iso_datetime(signal.signal_time_utc) <= as_of
        and parse_iso_datetime(signal.signal_time_utc) >= cutoff
    ]


def find_retrace_signals(
    bars: Sequence[Bar],
    *,
    config: StrategyConfig,
) -> list[Signal]:
    """Confirm each wickless setup on its first qualifying later 15m bar.

    A gap in the 15-minute sequence cancels the setup rather than allowing a
    stale Friday signal, for example, to remain pending into the next session.
    """

    ordered = sorted(bars, key=lambda item: item.timestamp)
    signals: list[Signal] = []
    step = timedelta(minutes=TIMEFRAME_MINUTES)
    for index, wickless_bar in enumerate(ordered):
        pattern = classify_wickless(
            wickless_bar,
            tick_size=config.profile.tick_size,
            tolerance_ticks=config.tolerance_ticks,
        )
        if pattern is None:
            continue
        for offset in range(1, config.retrace_bars + 1):
            candidate_index = index + offset
            if candidate_index >= len(ordered):
                break
            retrace_bar = ordered[candidate_index]
            if retrace_bar.timestamp != wickless_bar.timestamp + offset * step:
                break
            signal = build_signal(
                wickless_bar,
                config,
                retrace_bar=retrace_bar,
                retrace_bar_number=offset,
            )
            if signal is not None:
                signals.append(signal)
                break
    return signals


def _trade_from_exit(
    signal: Signal,
    *,
    exit_time: datetime,
    exit_price: float,
    reason: str,
    config: StrategyConfig,
) -> Trade:
    direction = 1 if signal.side == "BUY" else -1
    gross = direction * (exit_price - signal.entry_reference)
    risk = signal.risk_points
    round_trip_cost = (
        2 * config.commission_per_side
        + 2 * config.slippage_ticks * config.profile.tick_size
    )
    net = gross - round_trip_cost
    return Trade(
        signal_key=signal.key,
        instrument=signal.instrument,
        side=signal.side,
        pattern=signal.pattern,
        entry_time_utc=signal.signal_time_utc,
        exit_time_utc=exit_time.astimezone(UTC).isoformat(),
        entry=signal.entry_reference,
        exit=_price(exit_price, config.profile),
        stop=signal.stop,
        target=signal.target,
        exit_reason=reason,
        realized_r=round(gross / risk, 4),
        gross_points=_price(gross, config.profile),
        net_points_after_costs=_price(net, config.profile),
    )


def run_backtest(
    bars: Sequence[Bar],
    *,
    config: StrategyConfig,
    start: datetime | None = None,
    end: datetime | None = None,
) -> BacktestResult:
    """Backtest one position at a time with conservative same-bar exits."""

    start = (start or datetime.min.replace(tzinfo=UTC)).astimezone(UTC)
    end = (end or datetime.max.replace(tzinfo=UTC)).astimezone(UTC)
    if start >= end:
        raise ValueError("start must be before end")

    confirmed_by_bar: dict[datetime, list[Signal]] = {}
    for signal in find_retrace_signals(bars, config=config):
        wickless_time = parse_iso_datetime(signal.bar_open_time_utc)
        retrace_time = parse_iso_datetime(signal.retrace_bar_open_time_utc)
        if wickless_time < start or retrace_time >= end:
            continue
        confirmed_by_bar.setdefault(retrace_time, []).append(signal)

    result = BacktestResult(signals=[], trades=[], open_signal=None)
    position: Signal | None = None
    for bar in bars:
        if bar.timestamp < start or bar.timestamp >= end:
            continue

        if position is not None:
            stop_hit = (
                bar.low <= position.stop
                if position.side == "BUY"
                else bar.high >= position.stop
            )
            target_hit = (
                bar.high >= position.target
                if position.side == "BUY"
                else bar.low <= position.target
            )
            if stop_hit or target_hit:
                if stop_hit:
                    reason = "STOP_AMBIGUOUS" if target_hit else "STOP"
                    exit_price = position.stop
                    result.ambiguous_exits += int(target_hit)
                else:
                    reason = "TARGET"
                    exit_price = position.target
                result.trades.append(
                    _trade_from_exit(
                        position,
                        exit_time=bar.timestamp,
                        exit_price=exit_price,
                        reason=reason,
                        config=config,
                    )
                )
                position = None

        if position is None and confirmed_by_bar.get(bar.timestamp):
            signal = confirmed_by_bar[bar.timestamp][0]
            result.signals.append(signal)
            position = signal

    result.open_signal = position
    return result


def summarize_backtest(
    result: BacktestResult,
    *,
    config: StrategyConfig,
    data_file: Path,
    start: datetime,
    end: datetime,
) -> dict[str, object]:
    trades = result.trades
    net = [trade.net_points_after_costs for trade in trades]
    winners = [value for value in net if value > 0]
    losers = [value for value in net if value < 0]
    equity = peak = max_drawdown = 0.0
    for value in net:
        equity += value
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    profit_factor = sum(winners) / -sum(losers) if losers else None
    return {
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "source": {
            "implementation": "independent public-behavior reimplementation",
            "data_file": data_file.name,
            "instrument": config.profile.symbol,
            "timeframe": TIMEFRAME_LABEL,
        },
        "window": {
            "start_utc": start.astimezone(UTC).isoformat(),
            "end_utc_exclusive": end.astimezone(UTC).isoformat(),
        },
        "configuration": {
            "entry": "confirmation candle close after origin-price retrace",
            "direction": "with the wickless candle (bullish BUY, bearish SELL)",
            "reward_risk": config.reward_risk,
            "tolerance_ticks": config.tolerance_ticks,
            "retrace_bars": config.retrace_bars,
            "retrace_margin_percent": config.retrace_margin_percent,
            "stop_buffer_ticks": config.effective_stop_buffer_ticks,
            "slippage_ticks_per_side": config.slippage_ticks,
            "commission_points_per_side": config.commission_per_side,
            "same_bar_stop_and_target": "stop first",
            "max_concurrent_positions": 1,
        },
        "results": {
            "signals": len(result.signals),
            "closed_trades": len(trades),
            "open_trade_at_end": int(result.open_signal is not None),
            "wins": len(winners),
            "losses": len(losers),
            "win_rate_percent": (
                round(100 * len(winners) / len(trades), 2) if trades else None
            ),
            "net_points_after_costs": round(sum(net), config.profile.price_decimals),
            "profit_factor": round(profit_factor, 4) if profit_factor else None,
            "average_realized_r": (
                round(statistics.mean(trade.realized_r for trade in trades), 4)
                if trades
                else None
            ),
            "max_closed_trade_drawdown_points": round(
                max_drawdown, config.profile.price_decimals
            ),
            "ambiguous_exits_counted_stop_first": result.ambiguous_exits,
        },
        "limitations": [
            "Bid-only OHLC does not model the live ask spread.",
            "A fifteen-minute bar cannot reveal the order of intrabar "
            "stop/target touches.",
            "The protected TradingView source was not accessed or copied.",
            "The original indicator publishes no position-sizing or bracket rules; "
            "the documented 2R execution layer is this project's strategy layer.",
        ],
    }


def write_backtest(
    output_dir: Path,
    summary: dict[str, object],
    trades: Iterable[Trade],
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    rows = [asdict(trade) for trade in trades]
    with (output_dir / "trades.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(Trade.__dataclass_fields__))
        writer.writeheader()
        writer.writerows(rows)


def validate_webhook_url(url: str) -> str:
    candidate = url.strip()
    parsed = urllib.parse.urlparse(candidate)
    if parsed.scheme != "https" or parsed.hostname not in {
        "discord.com",
        "discordapp.com",
    }:
        raise ValueError("Expected an HTTPS Discord webhook URL")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 4 or parts[:2] != ["api", "webhooks"]:
        raise ValueError("Invalid Discord /api/webhooks/<id>/<token> path")
    if not parts[2].isdigit() or not parts[3]:
        raise ValueError("Discord webhook ID or token is missing")
    if parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ValueError("Discord webhook URL contains unsupported components")
    return candidate


def load_current_quote(path: Path, *, instrument: str) -> CurrentQuote:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("instrument") != instrument:
        raise ValueError(f"{path} does not contain a {instrument} quote")
    try:
        quote = CurrentQuote(
            instrument=instrument,
            observed_time_utc=parse_iso_datetime(
                str(raw["observed_time_utc"])
            ).isoformat(),
            bid=float(raw["bid"]),
            ask=float(raw["ask"]),
            spread=float(raw["spread"]),
            source=str(raw["source"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(f"{path} contains an invalid quote: {error}") from error
    if not all(math.isfinite(value) and value > 0 for value in (quote.bid, quote.ask)):
        raise ValueError(f"{path} contains invalid bid/ask prices")
    if quote.ask < quote.bid or quote.spread < 0:
        raise ValueError(f"{path} contains a negative spread")
    if not math.isclose(
        quote.ask - quote.bid,
        quote.spread,
        rel_tol=1e-6,
        abs_tol=INSTRUMENTS[instrument].tick_size / 2,
    ):
        raise ValueError(f"{path} spread does not match ask minus bid")
    return quote


def _bar_at(bars: Sequence[Bar], timestamp: datetime) -> Bar | None:
    return next((bar for bar in bars if bar.timestamp == timestamp), None)


def _price_path_status(
    signal: OriginLimitSignal,
    *,
    bid_bars: Sequence[Bar],
    ask_bars: Sequence[Bar],
    require_ask_fill: bool,
) -> str | None:
    fill_open = parse_iso_datetime(signal.fill_bar_open_time_utc)
    if (
        signal.entry_model == "origin_limit"
        and signal.side == "BUY"
        and require_ask_fill
    ):
        ask_fill_bar = _bar_at(ask_bars, fill_open)
        if ask_fill_bar is None or ask_fill_bar.low > signal.entry_reference:
            return ASK_FILL_NOT_CONFIRMED

    exit_bars = bid_bars if signal.side == "BUY" else ask_bars
    stop_seen = target_seen = False
    for bar in exit_bars:
        if bar.timestamp < fill_open or (
            signal.entry_model == "zone_reclaim" and bar.timestamp == fill_open
        ):
            continue
        if signal.side == "BUY":
            stop_hit = bar.low <= signal.stop
            target_hit = bar.high >= signal.target
        else:
            stop_hit = bar.high >= signal.stop
            target_hit = bar.low <= signal.target
        if stop_hit and target_hit:
            return AMBIGUOUS_PRICE_PATH
        stop_seen = stop_seen or stop_hit
        target_seen = target_seen or target_hit
        if stop_seen and target_seen:
            return AMBIGUOUS_PRICE_PATH
        if stop_seen:
            return STOP_ALREADY_REACHED
        if target_seen:
            return TARGET_ALREADY_REACHED
    return None


def validate_signal_actionability(
    signal: OriginLimitSignal,
    *,
    bid_bars: Sequence[Bar],
    ask_bars: Sequence[Bar],
    quote: CurrentQuote,
    as_of: datetime,
    max_signal_age_seconds: int = DEFAULT_MAX_SIGNAL_AGE_SECONDS,
    max_quote_age_seconds: int = DEFAULT_MAX_QUOTE_AGE_SECONDS,
    max_entry_deviation_r: float = DEFAULT_MAX_ENTRY_DEVIATION_R,
) -> OriginLimitSignal:
    """Fail closed unless a historical fill is still executable right now."""

    if max_signal_age_seconds < 1 or max_quote_age_seconds < 1:
        raise ValueError("signal and quote age limits must be positive")
    if max_entry_deviation_r < 0:
        raise ValueError("max_entry_deviation_r cannot be negative")
    if quote.instrument != signal.instrument:
        raise ValueError("quote instrument does not match signal")
    as_of = as_of.astimezone(UTC)
    fill_time = parse_iso_datetime(signal.fill_time_utc)
    quote_time = parse_iso_datetime(quote.observed_time_utc)
    signal_age = max(0, math.ceil((as_of - fill_time).total_seconds()))
    quote_age = (as_of - quote_time).total_seconds()
    current_price = quote.ask if signal.side == "BUY" else quote.bid
    directional_distance = (
        current_price - signal.entry_reference
        if signal.side == "BUY"
        else signal.entry_reference - current_price
    )
    distance_r = directional_distance / signal.risk_points
    profile = INSTRUMENTS[signal.instrument]
    atr_15m = signal.atr_15m
    if atr_15m <= 0:
        fill_open = parse_iso_datetime(signal.fill_bar_open_time_utc)
        known_bars = [bar for bar in bid_bars if bar.timestamp <= fill_open]
        if not known_bars:
            raise ValueError("Cannot evaluate risk without a fill-time ATR")
        atr_15m = atr_series(known_bars, DEFAULT_ATR_PERIOD)[-1]
    risk_status, risk_metrics = evaluate_risk_integrity(
        profile=profile,
        risk_distance=signal.risk_points,
        atr_15m=atr_15m,
        spread=quote.spread,
        slippage_ticks_per_side=signal.slippage_ticks_per_side,
    )
    current_stop_reached = (
        quote.bid <= signal.stop
        if signal.side == "BUY"
        else quote.ask >= signal.stop
    )
    current_target_reached = (
        quote.bid >= signal.target
        if signal.side == "BUY"
        else quote.ask <= signal.target
    )
    status = ACTIONABLE
    if fill_time > as_of or signal_age > max_signal_age_seconds:
        status = EXPIRED_BY_AGE
    elif quote_time > as_of or quote_age > max_quote_age_seconds:
        status = STALE_QUOTE
    elif current_stop_reached and current_target_reached:
        status = AMBIGUOUS_PRICE_PATH
    elif current_stop_reached:
        status = STOP_ALREADY_REACHED
    elif current_target_reached:
        status = TARGET_ALREADY_REACHED
    elif risk_status is not None:
        status = risk_status
    else:
        path_status = _price_path_status(
            signal,
            bid_bars=bid_bars,
            ask_bars=ask_bars,
            require_ask_fill=True,
        )
        if path_status is not None:
            status = path_status
        elif abs(distance_r) > max_entry_deviation_r:
            status = PRICE_MOVED_TOO_FAR
    return replace(
        signal,
        detected_time_utc=as_of.isoformat(),
        current_bid=_price(quote.bid, profile),
        current_ask=_price(quote.ask, profile),
        current_spread=_price(quote.spread, profile),
        distance_from_entry_points=_price(directional_distance, profile),
        distance_from_entry_r=round(distance_r, 4),
        signal_age_seconds=signal_age,
        actionability_status=status,
        risk_pips=round(risk_metrics["risk_pips"], 2),
        atr_15m=_price(risk_metrics["atr_15m"], profile),
        stop_distance_atr=round(risk_metrics["stop_distance_atr"], 4),
        minimum_stop_distance=_price(
            risk_metrics["minimum_stop_distance"], profile
        ),
        maximum_stop_distance=_price(
            risk_metrics["maximum_stop_distance"], profile
        ),
        minimum_stop_pips=risk_metrics["minimum_stop_pips"],
        spread_multiple=round(risk_metrics["spread_multiple"], 4),
        estimated_round_trip_cost=_price(
            risk_metrics["estimated_round_trip_cost"], profile
        ),
        cost_to_risk_ratio=round(risk_metrics["cost_to_risk_ratio"], 4),
    )


def discord_payload(signal: OriginLimitSignal) -> dict[str, object]:
    profile = INSTRUMENTS[signal.instrument]
    digits = profile.price_decimals
    pattern_label = signal.pattern.replace("_", " ").title()
    return {
        "username": f"Wickless {TIMEFRAME_LABEL} Signals",
        "allowed_mentions": {"parse": []},
        "embeds": [
            {
                "title": (
                    f"{signal.side} {signal.symbol} — Wickless "
                    f"{signal.timeframe}"
                ),
                "description": (
                    f"{pattern_label}; EMA({signal.ema_length}) trend confirmed "
                    "after a market-side origin-zone touch and directional reclaim."
                ),
                "color": 0x2ECC71 if signal.side == "BUY" else 0xE74C3C,
                "fields": [
                    {
                        "name": "Entry reference",
                        "value": f"`{signal.entry_reference:.{digits}f}`",
                        "inline": True,
                    },
                    {
                        "name": "Stop",
                        "value": f"`{signal.stop:.{digits}f}`",
                        "inline": True,
                    },
                    {
                        "name": f"Target ({signal.reward_risk:g}R)",
                        "value": f"`{signal.target:.{digits}f}`",
                        "inline": True,
                    },
                    {
                        "name": "Origin zone",
                        "value": (
                            f"`{signal.origin_zone_low:.{digits}f}–"
                            f"{signal.origin_zone_high:.{digits}f}`"
                        ),
                        "inline": True,
                    },
                    {
                        "name": "Setup quality",
                        "value": (
                            f"`{signal.quality_score:.1f}/100 • "
                            f"body {100 * signal.body_ratio:.0f}% • "
                            f"range {signal.wickless_range_atr:.2f} ATR`"
                        ),
                        "inline": True,
                    },
                    {
                        "name": "Touch / reclaim",
                        "value": (
                            f"`bar {signal.touch_bar_number} / "
                            f"bar {signal.confirmation_bar_number} • "
                            f"entry {signal.entry_displacement_atr:.2f} ATR from origin`"
                        ),
                        "inline": False,
                    },
                    {
                        "name": "Trend filter",
                        "value": (
                            f"`EMA {signal.ema_length} • "
                            f"slope {signal.ema_slope_lookback}`"
                        ),
                        "inline": True,
                    },
                    {
                        "name": "Pivot stop",
                        "value": (
                            f"`{signal.pivot_left}/{signal.pivot_right} "
                            "confirmed + 1 tick`"
                        ),
                        "inline": True,
                    },
                    {
                        "name": "Signal session",
                        "value": f"`{signal.session_label}`",
                        "inline": False,
                    },
                    {
                        "name": "Current BID / ASK",
                        "value": (
                            f"`{signal.current_bid:.{digits}f} / "
                            f"{signal.current_ask:.{digits}f}`"
                        ),
                        "inline": True,
                    },
                    {
                        "name": "Spread",
                        "value": f"`{signal.current_spread:.{digits}f}`",
                        "inline": True,
                    },
                    {
                        "name": "Risk distance",
                        "value": (
                            f"`{signal.risk_pips:.2f} pips • "
                            f"{signal.stop_distance_atr:.2f} ATR`"
                        ),
                        "inline": True,
                    },
                    {
                        "name": "Execution cost / 1R",
                        "value": (
                            f"`{100 * signal.cost_to_risk_ratio:.1f}% • "
                            f"spread cover {signal.spread_multiple:.1f}x`"
                        ),
                        "inline": True,
                    },
                    {
                        "name": "Entry deviation",
                        "value": (
                            f"`{signal.distance_from_entry_points:.{digits}f} • "
                            f"{signal.distance_from_entry_r:.2f}R`"
                        ),
                        "inline": True,
                    },
                    {
                        "name": "Actionability",
                        "value": (
                            f"`{signal.actionability_status} • "
                            f"age {signal.signal_age_seconds}s`"
                        ),
                        "inline": False,
                    },
                    {
                        "name": "Published (UTC)",
                        "value": f"`{signal.published_time_utc}`",
                        "inline": False,
                    },
                    {
                        "name": "Reclaim entry time (London)",
                        "value": f"`{signal.fill_time_london}`",
                        "inline": False,
                    },
                    {
                        "name": "Signal ID",
                        "value": f"`{signal.key}`",
                        "inline": False,
                    },
                ],
                "footer": {
                    "text": (
                        f"Dukascopy BID/ASK • finalized {signal.timeframe} candles • "
                        "origin touch + reclaim • research signal, not financial advice"
                    )
                },
                "timestamp": signal.fill_time_utc,
            }
        ],
    }


def post_discord(
    signal: OriginLimitSignal,
    webhook_url: str,
    *,
    timeout_seconds: float = 20,
) -> None:
    request = urllib.request.Request(
        validate_webhook_url(webhook_url),
        data=json.dumps(discord_payload(signal)).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "wickless-candle-bot/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            if response.status not in {200, 204}:
                raise RuntimeError(f"Discord returned HTTP {response.status}")
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"Discord returned HTTP {error.code}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"Could not reach Discord: {error.reason}") from error


def _load_state(path: Path) -> ScannerState:
    if not path.exists():
        return ScannerState(version=2, handled={}, positions={}, rejections=[])
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must contain a JSON object")
    # Migrate the v1 signal-id -> timestamp map without reposting old IDs.
    if "version" not in raw and all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in raw.items()
    ):
        return ScannerState(
            version=2,
            handled={
                key: {"status": "SENT_V1", "timestamp": timestamp}
                for key, timestamp in raw.items()
            },
            positions={},
            rejections=[],
        )
    if raw.get("version") != 2:
        raise ValueError(f"{path} has an unsupported scanner state version")
    handled = raw.get("handled")
    positions = raw.get("positions")
    rejections = raw.get("rejections")
    if not isinstance(handled, dict) or not isinstance(positions, dict):
        raise ValueError(f"{path} has invalid handled or position state")
    if not isinstance(rejections, list):
        raise ValueError(f"{path} has invalid rejection state")
    return ScannerState(
        version=2,
        handled=handled,
        positions=positions,
        rejections=rejections,
    )


def _save_state(path: Path, state: ScannerState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as handle:
        handle.write(json.dumps(asdict(state), indent=2, sort_keys=True) + "\n")
        temporary = Path(handle.name)
    temporary.replace(path)


def _latest_csv(data_dir: Path, instrument: str, *, side: str = "bid") -> Path:
    side = side.lower()
    if side not in {"bid", "ask"}:
        raise ValueError("side must be bid or ask")
    candidates = sorted(
        (
            path
            for path in data_dir.glob(
                f"{instrument}-{DATA_TIMEFRAME}-{side}-*.csv"
            )
            if path.stat().st_size > 0
        ),
        key=lambda path: (path.stat().st_mtime_ns, path.name),
        reverse=True,
    )
    if not candidates:
        raise ValueError(
            f"No {TIMEFRAME_MINUTES}-minute {side} CSV found for {instrument}"
        )
    return candidates[0]


def _record_rejection(
    state: ScannerState,
    signal: OriginLimitSignal,
    *,
    status: str,
    at: datetime,
) -> None:
    record = {
        "signal_id": signal.key,
        "instrument": signal.instrument,
        "side": signal.side,
        "status": status,
        "timestamp": at.astimezone(UTC).isoformat(),
        "signal_age_seconds": signal.signal_age_seconds,
        "distance_from_entry_r": signal.distance_from_entry_r,
        "risk_pips": signal.risk_pips,
        "atr_15m": signal.atr_15m,
        "stop_distance_atr": signal.stop_distance_atr,
        "minimum_stop_distance": signal.minimum_stop_distance,
        "maximum_stop_distance": signal.maximum_stop_distance,
        "spread_multiple": signal.spread_multiple,
        "estimated_round_trip_cost": signal.estimated_round_trip_cost,
        "cost_to_risk_ratio": signal.cost_to_risk_ratio,
        "entry_model": signal.entry_model,
        "origin_price": signal.origin_price,
        "origin_zone_low": signal.origin_zone_low,
        "origin_zone_high": signal.origin_zone_high,
        "touch_bar_number": signal.touch_bar_number,
        "confirmation_bar_number": signal.confirmation_bar_number,
        "body_ratio": signal.body_ratio,
        "wick_size_ticks": signal.wick_size_ticks,
        "wickless_range_atr": signal.wickless_range_atr,
        "close_location": signal.close_location,
        "quality_score": signal.quality_score,
        "entry_displacement_atr": signal.entry_displacement_atr,
    }
    state.rejections.append(record)
    state.rejections = state.rejections[-500:]
    state.handled[signal.key] = record


def _update_active_position(
    state: ScannerState,
    *,
    instrument: str,
    bid_bars: Sequence[Bar],
    ask_bars: Sequence[Bar],
    at: datetime,
) -> None:
    record = state.positions.get(instrument)
    if not isinstance(record, dict) or not isinstance(record.get("signal"), dict):
        return
    try:
        signal = OriginLimitSignal(**record["signal"])
    except TypeError as error:
        raise ValueError(f"Invalid persisted position for {instrument}: {error}") from error
    status = _price_path_status(
        signal,
        bid_bars=bid_bars,
        ask_bars=ask_bars,
        require_ask_fill=False,
    )
    if status is None:
        record["last_evaluated_time_utc"] = at.astimezone(UTC).isoformat()
        return
    state.positions.pop(instrument, None)
    handled = state.handled.setdefault(signal.key, {})
    handled["position_status"] = status
    handled["closed_time_utc"] = at.astimezone(UTC).isoformat()


def scan_markets(
    *,
    data_dir: Path,
    instruments: Sequence[str],
    state_path: Path,
    as_of: datetime,
    max_signal_age_seconds: int,
    state_retention_days: int,
    webhook_url: str | None,
    dry_run: bool = False,
    max_quote_age_seconds: int = DEFAULT_MAX_QUOTE_AGE_SECONDS,
    max_entry_deviation_r: float = DEFAULT_MAX_ENTRY_DEVIATION_R,
    research_lookback_seconds: int = DEFAULT_RESEARCH_LOOKBACK_SECONDS,
) -> tuple[int, int]:
    """Publish only fresh, executable, unique signals and persist position state."""

    if not dry_run:
        if not webhook_url:
            raise RuntimeError(
                "DISCORD_WEBHOOK_URL is missing. Add it as a repository secret "
                "or environment variable."
            )
        validate_webhook_url(webhook_url)
    as_of = as_of.astimezone(UTC)
    if research_lookback_seconds < max_signal_age_seconds:
        raise ValueError(
            "research_lookback_seconds cannot be shorter than the actionable age"
        )
    state = _load_state(state_path)
    retention_cutoff = as_of - timedelta(days=state_retention_days)
    state.handled = {
        key: record
        for key, record in state.handled.items()
        if isinstance(record, dict)
        and isinstance(record.get("timestamp"), str)
        and parse_iso_datetime(str(record["timestamp"])) >= retention_cutoff
    }

    found = posted = 0
    for instrument in instruments:
        bid_bars = load_bars(_latest_csv(data_dir, instrument, side="bid"))
        ask_bars = load_bars(_latest_csv(data_dir, instrument, side="ask"))
        quote = load_current_quote(
            data_dir / f"{instrument}-quote-live.json",
            instrument=instrument,
        )
        _update_active_position(
            state,
            instrument=instrument,
            bid_bars=bid_bars,
            ask_bars=ask_bars,
            at=as_of,
        )
        signals = find_fresh_origin_limit_signals(
            bid_bars,
            instrument=instrument,
            as_of=as_of,
            max_signal_age_seconds=research_lookback_seconds,
            ask_bars=ask_bars,
        )
        if not signals:
            print(f"{INSTRUMENTS[instrument].symbol}: no fresh origin-reclaim entry")
            continue
        for signal in signals:
            found += 1
            if signal.key in state.handled:
                print(f"{signal.symbol}: already sent {signal.key}")
                continue
            validated = validate_signal_actionability(
                signal,
                bid_bars=bid_bars,
                ask_bars=ask_bars,
                quote=quote,
                as_of=as_of,
                max_signal_age_seconds=max_signal_age_seconds,
                max_quote_age_seconds=max_quote_age_seconds,
                max_entry_deviation_r=max_entry_deviation_r,
            )
            if instrument in state.positions:
                validated = replace(
                    validated,
                    actionability_status=ACTIVE_POSITION_EXISTS,
                )
            if validated.actionability_status != ACTIONABLE:
                _record_rejection(
                    state,
                    validated,
                    status=validated.actionability_status,
                    at=as_of,
                )
                _save_state(state_path, state)
                print(
                    f"{signal.symbol}: rejected {signal.key} "
                    f"({validated.actionability_status})"
                )
                continue
            publishable = replace(validated, published_time_utc=as_of.isoformat())
            # Claim before the network call. A crash can suppress one signal, but
            # cannot produce a duplicate Discord trade alert on retry.
            state.handled[signal.key] = {
                "status": "PUBLISHING",
                "timestamp": as_of.isoformat(),
                "signal": asdict(publishable),
            }
            _save_state(state_path, state)
            if dry_run:
                print(json.dumps(asdict(publishable), indent=2))
            else:
                assert webhook_url is not None
                try:
                    post_discord(publishable, webhook_url)
                except RuntimeError:
                    state.handled[signal.key]["status"] = "DELIVERY_FAILED"
                    _save_state(state_path, state)
                    raise
            state.handled[signal.key]["status"] = "DRY_RUN" if dry_run else "SENT"
            state.positions[instrument] = {
                "status": "OPEN",
                "opened_time_utc": as_of.isoformat(),
                "last_evaluated_time_utc": as_of.isoformat(),
                "signal": asdict(publishable),
            }
            _save_state(state_path, state)
            posted += 1
            print(f"{signal.symbol}: {'would send' if dry_run else 'sent'} {signal.key}")

    _save_state(state_path, state)
    return found, posted


def command_backtest(args: argparse.Namespace) -> int:
    from no_wick_research import (
        NoWickConfig,
        run_no_wick_backtest,
        summarize_result,
    )

    config = NoWickConfig(
        instrument=args.instrument,
        reward_risk=args.reward_risk,
        tolerance_ticks=args.tolerance_ticks,
        ema_length=args.ema_length,
        ema_slope_lookback=args.ema_slope_lookback,
        pivot_left=args.pivot_left,
        pivot_right=args.pivot_right,
        stop_buffer_ticks=args.stop_buffer_ticks,
        slippage_ticks=args.slippage_ticks,
    )
    start = parse_iso_datetime(args.start)
    end = parse_iso_datetime(args.end)
    bid_bars = load_bars(args.csv)
    ask_bars = load_bars(args.ask_csv) if args.ask_csv else None
    result = run_no_wick_backtest(
        bid_bars,
        config=config,
        ask_bars=ask_bars,
        start=start,
        end=end,
    )
    results = summarize_result(result)
    summary = {
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "source": {
            "data_file": args.csv.name,
            "instrument": config.profile.symbol,
            "timeframe": TIMEFRAME_LABEL,
            "ask_data_file": args.ask_csv.name if args.ask_csv else None,
        },
        "window": {
            "start_utc": start.isoformat(),
            "end_utc_exclusive": end.isoformat(),
        },
        "configuration": {
            "trend_filter": (
                f"close vs EMA({config.ema_length}) + "
                f"{config.ema_slope_lookback}-bar slope"
            ),
            "signal_session": (
                f"{config.session_start.strftime('%H:%M')}–"
                f"{config.session_end.strftime('%H:%M')} America/New_York"
            ),
            "entry": "market-side close after origin-zone touch and directional reclaim",
            "stop": (
                f"latest confirmed {config.pivot_left}/{config.pivot_right} "
                f"pivot plus {config.stop_buffer_ticks} tick"
            ),
            "reward_risk": config.reward_risk,
            "pending_expiry": f"{config.expiry_bars} bars",
            "origin_zone_atr_fraction": config.origin_zone_atr_fraction,
            "minimum_body_ratio": config.minimum_body_ratio,
            "wickless_range_atr": [
                config.minimum_range_atr,
                config.maximum_range_atr,
            ],
            "maximum_entry_displacement_atr": (
                config.maximum_entry_displacement_atr
            ),
            "pending_orders": "multiple allowed",
            "slippage_ticks_per_side": config.slippage_ticks,
            "same_bar_ambiguity": "stop first",
            "market_sides": (
                "BUY ask->bid; SELL bid->ask"
                if args.ask_csv
                else "legacy bid-only approximation"
            ),
        },
        "results": results,
    }
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    rows = [asdict(trade) for trade in result.trades]
    with (args.output / "trades.csv").open(
        "w", newline="", encoding="utf-8"
    ) as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=list(rows[0]) if rows else [
                "order_id",
                "instrument",
                "side",
                "pattern",
                "signal_time_utc",
                "entry_time_utc",
                "exit_time_utc",
                "entry",
                "stop",
                "target",
                "exit",
                "exit_reason",
                "gross_r",
                "net_r_after_costs",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)
    print(json.dumps(results, indent=2))
    return 0


def command_scan(args: argparse.Namespace) -> int:
    as_of = parse_iso_datetime(args.as_of) if args.as_of else datetime.now(UTC)
    found, posted = scan_markets(
        data_dir=args.data_dir,
        instruments=args.instruments,
        state_path=args.state,
        as_of=as_of,
        max_signal_age_seconds=args.max_signal_age_seconds,
        state_retention_days=args.state_retention_days,
        webhook_url=os.getenv("DISCORD_WEBHOOK_URL"),
        dry_run=args.dry_run,
        max_quote_age_seconds=args.max_quote_age_seconds,
        max_entry_deviation_r=args.max_entry_deviation_r,
        research_lookback_seconds=args.research_lookback_seconds,
    )
    print(f"Scan complete: {found} fresh, {posted} newly handled")
    return 0


def command_stats(args: argparse.Namespace) -> int:
    config = StrategyConfig(
        instrument=args.instrument,
        tolerance_ticks=args.tolerance_ticks,
    )
    bars = load_bars(args.csv)
    bullish = bearish = bullish_wickless = bearish_wickless = 0
    for bar in bars:
        bullish += int(bar.close > bar.open)
        bearish += int(bar.close < bar.open)
        pattern = classify_wickless(
            bar,
            tick_size=config.profile.tick_size,
            tolerance_ticks=config.tolerance_ticks,
        )
        bullish_wickless += int(
            pattern is not None and pattern.kind == "BULLISH_WICKLESS"
        )
        bearish_wickless += int(
            pattern is not None and pattern.kind == "BEARISH_WICKLESS"
        )
    payload = {
        "instrument": config.profile.symbol,
        "timeframe": TIMEFRAME_LABEL,
        "bars": len(bars),
        "bullish_candles": bullish,
        "bearish_candles": bearish,
        "bullish_wickless": bullish_wickless,
        "bearish_wickless": bearish_wickless,
        "bullish_wickless_percent": (
            round(100 * bullish_wickless / bullish, 4) if bullish else None
        ),
        "bearish_wickless_percent": (
            round(100 * bearish_wickless / bearish, 4) if bearish else None
        ),
    }
    print(json.dumps(payload, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan = subparsers.add_parser(
        "scan",
        help=f"Post unseen fresh {TIMEFRAME_LABEL} signals",
    )
    scan.add_argument("--data-dir", required=True, type=Path)
    scan.add_argument("--state", type=Path, default=Path(".signal-state/seen.json"))
    scan.add_argument("--as-of")
    scan.add_argument(
        "--max-signal-age-seconds",
        type=int,
        default=DEFAULT_MAX_SIGNAL_AGE_SECONDS,
    )
    scan.add_argument(
        "--max-quote-age-seconds",
        type=int,
        default=DEFAULT_MAX_QUOTE_AGE_SECONDS,
    )
    scan.add_argument(
        "--max-entry-deviation-r",
        type=float,
        default=DEFAULT_MAX_ENTRY_DEVIATION_R,
    )
    scan.add_argument(
        "--research-lookback-seconds",
        type=int,
        default=DEFAULT_RESEARCH_LOOKBACK_SECONDS,
    )
    scan.add_argument("--state-retention-days", type=int, default=14)
    scan.add_argument(
        "--instruments",
        nargs="+",
        choices=tuple(INSTRUMENTS),
        default=list(LIVE_INSTRUMENTS),
    )
    scan.add_argument("--dry-run", action="store_true")
    scan.set_defaults(func=command_scan)

    backtest = subparsers.add_parser(
        "backtest",
        help=f"Backtest a {TIMEFRAME_LABEL} OHLC CSV",
    )
    backtest.add_argument("--csv", required=True, type=Path)
    backtest.add_argument("--ask-csv", type=Path)
    backtest.add_argument("--start", required=True)
    backtest.add_argument("--end", required=True)
    backtest.add_argument("--output", type=Path, default=Path("reports/latest"))
    backtest.add_argument("--instrument", choices=tuple(INSTRUMENTS), default="eurusd")
    backtest.add_argument("--reward-risk", type=float, default=2.0)
    backtest.add_argument("--tolerance-ticks", type=float, default=0.5)
    backtest.add_argument("--ema-length", type=int, default=50)
    backtest.add_argument("--ema-slope-lookback", type=int, default=5)
    backtest.add_argument("--pivot-left", type=int, default=3)
    backtest.add_argument("--pivot-right", type=int, default=3)
    backtest.add_argument("--stop-buffer-ticks", type=int, default=1)
    backtest.add_argument("--slippage-ticks", type=float, default=1.0)
    backtest.set_defaults(func=command_backtest)

    stats = subparsers.add_parser("stats", help="Reproduce indicator statistics")
    stats.add_argument("--csv", required=True, type=Path)
    stats.add_argument("--instrument", choices=tuple(INSTRUMENTS), default="eurusd")
    stats.add_argument("--tolerance-ticks", type=float, default=0.5)
    stats.set_defaults(func=command_stats)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as error:
        parser.error(str(error))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
