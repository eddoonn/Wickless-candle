#!/usr/bin/env python3
"""Five-minute wickless-candle backtester and Discord signal bot.

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
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Sequence
from zoneinfo import ZoneInfo


UTC = timezone.utc
LONDON = ZoneInfo("Europe/London")
TIMEFRAME_MINUTES = 5


@dataclass(frozen=True)
class InstrumentProfile:
    key: str
    symbol: str
    tick_size: float
    price_decimals: int
    stop_buffer_ticks: int
    jetta_code: str


INSTRUMENTS = {
    "xauusd": InstrumentProfile("xauusd", "XAUUSD", 0.001, 3, 20, "XAU-USD"),
    "eurusd": InstrumentProfile("eurusd", "EURUSD", 0.00001, 5, 20, "EUR-USD"),
    "gbpusd": InstrumentProfile("gbpusd", "GBPUSD", 0.00001, 5, 20, "GBP-USD"),
    "usdjpy": InstrumentProfile("usdjpy", "USDJPY", 0.001, 3, 20, "USD-JPY"),
    "usdchf": InstrumentProfile("usdchf", "USDCHF", 0.00001, 5, 20, "USD-CHF"),
    "usdcad": InstrumentProfile("usdcad", "USDCAD", 0.00001, 5, 20, "USD-CAD"),
    "audusd": InstrumentProfile("audusd", "AUDUSD", 0.00001, 5, 20, "AUD-USD"),
    "nzdusd": InstrumentProfile("nzdusd", "NZDUSD", 0.00001, 5, 20, "NZD-USD"),
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
    instrument: str = "eurusd"
    reward_risk: float = 3.0
    tolerance_ticks: float = 0.5
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


def classify_wickless(
    bar: Bar,
    *,
    tick_size: float,
    tolerance_ticks: float = 0.5,
) -> WicklessPattern | None:
    """Classify a directional candle with no wick from its opening price.

    Bullish ``open == low`` candles are missing a lower wick and therefore
    produce a SELL/retracement signal.  Bearish ``open == high`` candles are
    missing an upper wick and produce a BUY/retracement signal.
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
            signal_side="SELL",
        )
    if bar.close < bar.open and bar.high - bar.open <= tolerance:
        return WicklessPattern(
            kind="BEARISH_WICKLESS",
            missing_wick="UPPER",
            signal_side="BUY",
        )
    return None


def _price(value: float, profile: InstrumentProfile) -> float:
    return round(value, profile.price_decimals)


def build_signal(bar: Bar, config: StrategyConfig) -> Signal | None:
    pattern = classify_wickless(
        bar,
        tick_size=config.profile.tick_size,
        tolerance_ticks=config.tolerance_ticks,
    )
    if pattern is None:
        return None

    entry = bar.close
    if pattern.signal_side == "SELL":
        stop = bar.high + config.stop_buffer
        risk = stop - entry
        target = entry - config.reward_risk * risk
    else:
        stop = bar.low - config.stop_buffer
        risk = entry - stop
        target = entry + config.reward_risk * risk
    if risk <= config.profile.tick_size / 2:
        return None

    signal_time = bar.close_time.astimezone(UTC)
    identity = (
        f"wickless-v1|{config.instrument}|{bar.timestamp.isoformat()}|"
        f"{pattern.kind}|5m"
    )
    key = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    profile = config.profile
    return Signal(
        key=key,
        instrument=config.instrument,
        symbol=profile.symbol,
        timeframe="5m",
        pattern=pattern.kind,
        missing_wick=pattern.missing_wick,
        side=pattern.signal_side,
        bar_open_time_utc=bar.timestamp.astimezone(UTC).isoformat(),
        signal_time_utc=signal_time.isoformat(),
        signal_time_london=signal_time.astimezone(LONDON).isoformat(),
        entry_reference=_price(entry, profile),
        stop=_price(stop, profile),
        target=_price(target, profile),
        trigger_level=_price(bar.open, profile),
        candle_open=_price(bar.open, profile),
        candle_high=_price(bar.high, profile),
        candle_low=_price(bar.low, profile),
        candle_close=_price(bar.close, profile),
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
                    f"{path}:{line_number}: timestamp is not aligned to 5 minutes"
                )
            if bar.high == bar.low:
                continue
            bars_by_time[bar.timestamp] = bar
    return [bars_by_time[key] for key in sorted(bars_by_time)]


def find_fresh_signals(
    bars: Sequence[Bar],
    *,
    config: StrategyConfig,
    as_of: datetime,
    max_signal_age_minutes: int = 20,
) -> list[Signal]:
    as_of = as_of.astimezone(UTC)
    if max_signal_age_minutes < TIMEFRAME_MINUTES:
        raise ValueError("max_signal_age_minutes must be at least 5")
    cutoff = as_of - timedelta(minutes=max_signal_age_minutes)
    signals: list[Signal] = []
    for bar in bars:
        if bar.close_time > as_of or bar.close_time < cutoff:
            continue
        signal = build_signal(bar, config)
        if signal is not None:
            signals.append(signal)
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

        if position is None:
            signal = build_signal(bar, config)
            if signal is not None:
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
            "timeframe": "5m",
        },
        "window": {
            "start_utc": start.astimezone(UTC).isoformat(),
            "end_utc_exclusive": end.astimezone(UTC).isoformat(),
        },
        "configuration": {
            "entry": "signal candle close",
            "direction": "toward the missing opening-side wick",
            "reward_risk": config.reward_risk,
            "tolerance_ticks": config.tolerance_ticks,
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
            "A five-minute bar cannot reveal the order of intrabar stop/target touches.",
            "The protected TradingView source was not accessed or copied.",
            "The original indicator publishes no position-sizing or bracket rules; "
            "the documented 3R execution layer is this project's strategy layer.",
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


def discord_payload(signal: Signal) -> dict[str, object]:
    profile = INSTRUMENTS[signal.instrument]
    digits = profile.price_decimals
    pattern_label = signal.pattern.replace("_", " ").title()
    return {
        "username": "Wickless 5m Signals",
        "allowed_mentions": {"parse": []},
        "embeds": [
            {
                "title": f"{signal.side} {signal.symbol} — Wickless 5m",
                "description": (
                    f"{pattern_label}; the setup targets the missing "
                    f"{signal.missing_wick.lower()} wick."
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
                        "name": "Missing-wick trigger",
                        "value": f"`{signal.trigger_level:.{digits}f}`",
                        "inline": True,
                    },
                    {
                        "name": "London time",
                        "value": f"`{signal.signal_time_london}`",
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
                        "Dukascopy bid • finalized 5m candle • research signal, "
                        "not financial advice"
                    )
                },
                "timestamp": signal.signal_time_utc,
            }
        ],
    }


def post_discord(
    signal: Signal,
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


def _load_state(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in raw.items()
    ):
        raise ValueError(f"{path} must contain a JSON object of signal timestamps")
    return raw


def _save_state(path: Path, state: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as handle:
        handle.write(json.dumps(state, indent=2, sort_keys=True) + "\n")
        temporary = Path(handle.name)
    temporary.replace(path)


def _latest_csv(data_dir: Path, instrument: str) -> Path:
    candidates = sorted(
        (
            path
            for path in data_dir.glob(f"{instrument}-m5-bid-*.csv")
            if path.stat().st_size > 0
        ),
        key=lambda path: (path.stat().st_mtime_ns, path.name),
        reverse=True,
    )
    if not candidates:
        raise ValueError(f"No 5-minute bid CSV found for {instrument}")
    return candidates[0]


def scan_markets(
    *,
    data_dir: Path,
    instruments: Sequence[str],
    state_path: Path,
    as_of: datetime,
    max_signal_age_minutes: int,
    state_retention_days: int,
    webhook_url: str | None,
    dry_run: bool = False,
) -> tuple[int, int]:
    """Post every unseen fresh signal and atomically persist its ID."""

    if not dry_run:
        if not webhook_url:
            raise RuntimeError(
                "DISCORD_WEBHOOK_URL is missing. Add it as a repository secret "
                "or environment variable."
            )
        validate_webhook_url(webhook_url)
    as_of = as_of.astimezone(UTC)
    state = _load_state(state_path)
    retention_cutoff = as_of - timedelta(days=state_retention_days)
    state = {
        key: timestamp
        for key, timestamp in state.items()
        if parse_iso_datetime(timestamp) >= retention_cutoff
    }

    found = posted = 0
    for instrument in instruments:
        config = StrategyConfig(instrument=instrument)
        bars = load_bars(_latest_csv(data_dir, instrument))
        signals = find_fresh_signals(
            bars,
            config=config,
            as_of=as_of,
            max_signal_age_minutes=max_signal_age_minutes,
        )
        if not signals:
            print(f"{config.profile.symbol}: no fresh wickless signal")
            continue
        for signal in signals:
            found += 1
            if signal.key in state:
                print(f"{signal.symbol}: already sent {signal.key}")
                continue
            if dry_run:
                print(json.dumps(asdict(signal), indent=2))
            else:
                assert webhook_url is not None
                post_discord(signal, webhook_url)
            state[signal.key] = signal.signal_time_utc
            _save_state(state_path, state)
            posted += 1
            print(f"{signal.symbol}: {'would send' if dry_run else 'sent'} {signal.key}")

    _save_state(state_path, state)
    return found, posted


def _config_from_args(args: argparse.Namespace) -> StrategyConfig:
    return StrategyConfig(
        instrument=args.instrument,
        reward_risk=args.reward_risk,
        tolerance_ticks=args.tolerance_ticks,
        stop_buffer_ticks=args.stop_buffer_ticks,
        slippage_ticks=args.slippage_ticks,
        commission_per_side=args.commission_per_side,
    )


def _strategy_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--instrument", choices=tuple(INSTRUMENTS), default="eurusd")
    parser.add_argument("--reward-risk", type=float, default=3.0)
    parser.add_argument("--tolerance-ticks", type=float, default=0.5)
    parser.add_argument("--stop-buffer-ticks", type=int)
    parser.add_argument("--slippage-ticks", type=float, default=1.0)
    parser.add_argument("--commission-per-side", type=float, default=0.0)


def command_backtest(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    start = parse_iso_datetime(args.start)
    end = parse_iso_datetime(args.end)
    result = run_backtest(
        load_bars(args.csv),
        config=config,
        start=start,
        end=end,
    )
    summary = summarize_backtest(
        result,
        config=config,
        data_file=args.csv,
        start=start,
        end=end,
    )
    write_backtest(args.output, summary, result.trades)
    print(json.dumps(summary["results"], indent=2))
    return 0


def command_scan(args: argparse.Namespace) -> int:
    as_of = parse_iso_datetime(args.as_of) if args.as_of else datetime.now(UTC)
    found, posted = scan_markets(
        data_dir=args.data_dir,
        instruments=args.instruments,
        state_path=args.state,
        as_of=as_of,
        max_signal_age_minutes=args.max_signal_age_minutes,
        state_retention_days=args.state_retention_days,
        webhook_url=os.getenv("DISCORD_WEBHOOK_URL"),
        dry_run=args.dry_run,
    )
    print(f"Scan complete: {found} fresh, {posted} newly handled")
    return 0


def command_stats(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
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
        "timeframe": "5m",
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

    scan = subparsers.add_parser("scan", help="Post unseen fresh 5m signals")
    scan.add_argument("--data-dir", required=True, type=Path)
    scan.add_argument("--state", type=Path, default=Path(".signal-state/seen.json"))
    scan.add_argument("--as-of")
    scan.add_argument("--max-signal-age-minutes", type=int, default=20)
    scan.add_argument("--state-retention-days", type=int, default=14)
    scan.add_argument(
        "--instruments",
        nargs="+",
        choices=tuple(INSTRUMENTS),
        default=list(LIVE_INSTRUMENTS),
    )
    scan.add_argument("--dry-run", action="store_true")
    scan.set_defaults(func=command_scan)

    backtest = subparsers.add_parser("backtest", help="Backtest a 5m OHLC CSV")
    backtest.add_argument("--csv", required=True, type=Path)
    backtest.add_argument("--start", required=True)
    backtest.add_argument("--end", required=True)
    backtest.add_argument("--output", type=Path, default=Path("reports/latest"))
    _strategy_arguments(backtest)
    backtest.set_defaults(func=command_backtest)

    stats = subparsers.add_parser("stats", help="Reproduce indicator statistics")
    stats.add_argument("--csv", required=True, type=Path)
    _strategy_arguments(stats)
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
