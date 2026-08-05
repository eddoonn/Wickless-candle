"""Leakage-safe decision-time features for Wickless setup meta-labeling."""

from __future__ import annotations

import math
from datetime import datetime, time, timezone
from typing import Any
from zoneinfo import ZoneInfo


UTC = timezone.utc
LONDON = ZoneInfo("Europe/London")
NEW_YORK = ZoneInfo("America/New_York")
FEATURE_SCHEMA_VERSION = "wickless-meta-label-features-v2"
SUPPORTED_FEATURE_SCHEMA_VERSIONS = (
    "wickless-meta-label-features-v1",
    FEATURE_SCHEMA_VERSION,
)
PAIR_KEYS = (
    "eurusd",
    "gbpusd",
    "usdjpy",
    "usdchf",
    "usdcad",
    "audusd",
    "nzdusd",
)
ENTRY_MODELS = ("signal_close", "zone_reclaim", "origin_limit")
SESSION_PHASES = ("london_only", "overlap", "new_york_only", "outside")
VOLATILITY_REGIMES = ("low", "normal", "high")
TREND_REGIMES = ("unknown", "moderate", "strong")
SPREAD_REGIMES = ("low", "normal", "elevated")

BASE_FEATURES = (
    "body_ratio",
    "wick_size_ticks",
    "range_atr",
    "close_location",
    "quality_score_fraction",
    "entry_displacement_atr",
    "stop_distance_atr",
    "cost_to_risk_ratio",
    "spread_multiple_log",
    "risk_pips_log",
    "touch_bar_number",
    "confirmation_bar_number",
    "ema_distance_atr",
    "ema_slope_atr",
    "recent_volatility_atr",
    "directional_persistence",
    "volatility_expansion_ratio",
    "correlated_signal_count",
    "hour_sin",
    "hour_cos",
    "side_buy",
)
FEATURE_NAMES = (
    *BASE_FEATURES,
    *(f"pair_{pair}" for pair in PAIR_KEYS),
    *(f"entry_{name}" for name in ENTRY_MODELS),
    *(f"session_{name}" for name in SESSION_PHASES),
    *(f"volatility_{name}" for name in VOLATILITY_REGIMES),
    *(f"trend_{name}" for name in TREND_REGIMES),
    *(f"spread_{name}" for name in SPREAD_REGIMES),
)


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _clock_fraction(moment: datetime) -> float:
    utc = moment.astimezone(UTC)
    return utc.hour + utc.minute / 60.0 + utc.second / 3600.0


def session_phase(moment: datetime) -> str:
    london = moment.astimezone(LONDON).time().replace(tzinfo=None)
    new_york = moment.astimezone(NEW_YORK).time().replace(tzinfo=None)
    london_active = time(8, 0) <= london < time(17, 0)
    new_york_active = time(5, 0) <= new_york < time(13, 30)
    if london_active and new_york_active:
        return "overlap"
    if london_active:
        return "london_only"
    if new_york_active:
        return "new_york_only"
    return "outside"


def _number(source: Any, name: str, fallback: float = 0.0) -> float:
    value = getattr(source, name, fallback)
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"Non-finite feature source: {name}")
    return number


def _volatility_regime(value: float) -> str:
    if value < 0.80:
        return "low"
    if value > 1.25:
        return "high"
    return "normal"


def _trend_regime(source: Any, distance: float, slope: float) -> str:
    has_context = hasattr(source, "ema_distance_atr") or hasattr(source, "ema_slope_atr")
    if not has_context:
        return "unknown"
    strength = abs(distance) + abs(slope)
    return "strong" if strength >= 0.75 else "moderate"


def _spread_regime(cost_to_risk: float) -> str:
    if cost_to_risk <= 0.025:
        return "low"
    if cost_to_risk >= 0.075:
        return "elevated"
    return "normal"


def features_from_setup(source: Any, *, instrument: str | None = None) -> dict[str, float]:
    """Return only fields observable when the setup is accepted for execution.

    Version 2 adds explicit volatility, trend, and spread regime context. Every
    added field is either supplied by the causal strategy engine or derived from
    already-approved execution-time measurements. Missing optional context is
    represented explicitly rather than inferred from future outcomes.
    """

    pair = (instrument or str(getattr(source, "instrument", ""))).lower()
    side = str(getattr(source, "side", "")).upper()
    entry_model = str(getattr(source, "entry_model", "signal_close"))
    timestamp_value = getattr(source, "fill_time_utc", None) or getattr(
        source, "entry_time_utc", None
    )
    if not timestamp_value:
        raise ValueError("Setup is missing a decision timestamp")
    timestamp = parse_time(str(timestamp_value))
    hour = _clock_fraction(timestamp)
    phase = session_phase(timestamp)

    range_atr = _number(source, "wickless_range_atr", _number(source, "range_atr"))
    spread_multiple = max(0.0, _number(source, "spread_multiple"))
    risk_pips = max(0.0, _number(source, "risk_pips"))
    cost_to_risk = min(1.0, max(0.0, _number(source, "cost_to_risk_ratio")))
    ema_distance = max(-5.0, min(5.0, _number(source, "ema_distance_atr")))
    ema_slope = max(-5.0, min(5.0, _number(source, "ema_slope_atr")))
    recent_volatility = max(
        0.0,
        min(10.0, _number(source, "recent_volatility_atr", range_atr)),
    )
    directional_persistence = max(
        -1.0,
        min(1.0, _number(source, "directional_persistence")),
    )
    volatility_expansion = max(
        0.0,
        min(10.0, _number(source, "volatility_expansion_ratio", 1.0)),
    )
    correlated_signals = max(
        0.0,
        min(float(len(PAIR_KEYS) - 1), _number(source, "correlated_signal_count")),
    )
    volatility = _volatility_regime(recent_volatility)
    trend = _trend_regime(source, ema_distance, ema_slope)
    spread = _spread_regime(cost_to_risk)

    values: dict[str, float] = {
        "body_ratio": _number(source, "body_ratio"),
        "wick_size_ticks": _number(source, "wick_size_ticks"),
        "range_atr": range_atr,
        "close_location": _number(source, "close_location"),
        "quality_score_fraction": _number(source, "quality_score") / 100.0,
        "entry_displacement_atr": _number(source, "entry_displacement_atr"),
        "stop_distance_atr": _number(source, "stop_distance_atr"),
        "cost_to_risk_ratio": cost_to_risk,
        "spread_multiple_log": math.log1p(min(spread_multiple, 1000.0)),
        "risk_pips_log": math.log1p(min(risk_pips, 10000.0)),
        "touch_bar_number": min(20.0, max(0.0, _number(source, "touch_bar_number"))),
        "confirmation_bar_number": min(
            20.0, max(0.0, _number(source, "confirmation_bar_number"))
        ),
        "ema_distance_atr": ema_distance,
        "ema_slope_atr": ema_slope,
        "recent_volatility_atr": recent_volatility,
        "directional_persistence": directional_persistence,
        "volatility_expansion_ratio": volatility_expansion,
        "correlated_signal_count": correlated_signals,
        "hour_sin": math.sin(2.0 * math.pi * hour / 24.0),
        "hour_cos": math.cos(2.0 * math.pi * hour / 24.0),
        "side_buy": 1.0 if side == "BUY" else 0.0,
    }
    values.update({f"pair_{name}": float(pair == name) for name in PAIR_KEYS})
    values.update(
        {f"entry_{name}": float(entry_model == name) for name in ENTRY_MODELS}
    )
    values.update(
        {f"session_{name}": float(phase == name) for name in SESSION_PHASES}
    )
    values.update(
        {f"volatility_{name}": float(volatility == name) for name in VOLATILITY_REGIMES}
    )
    values.update({f"trend_{name}": float(trend == name) for name in TREND_REGIMES})
    values.update({f"spread_{name}": float(spread == name) for name in SPREAD_REGIMES})
    missing = [name for name in FEATURE_NAMES if name not in values]
    if missing:
        raise RuntimeError("Feature construction is incomplete: " + ", ".join(missing))
    return {name: values[name] for name in FEATURE_NAMES}


def supported_instrument(instrument: str) -> bool:
    return instrument.lower() in PAIR_KEYS
