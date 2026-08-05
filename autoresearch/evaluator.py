#!/usr/bin/env python3
"""Immutable BID/ASK evaluator for constrained Wickless experiments."""

from __future__ import annotations

import ast
import csv
import gzip
import hashlib
import json
import math
import re
import statistics
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, replace
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from no_wick_research import NoWickConfig, run_no_wick_backtest
from wickless_bot import Bar, FOREX_MAJORS, INSTRUMENTS


UTC = timezone.utc
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY = Path(__file__).with_name("policy.json")
DEFAULT_CANDIDATE = Path(__file__).with_name("candidate.py")
NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")


class CandidateError(ValueError):
    """Raised when the editable candidate surface violates its contract."""


@dataclass(frozen=True)
class Candidate:
    name: str
    description: str
    parameters: dict[str, Any]
    source_sha256: str


@dataclass(frozen=True)
class Fold:
    name: str
    directory: str
    start: datetime
    end: datetime
    minimum_trades: int


IMMUTABLE_SAFETY = {
    "reward_risk": 2.0,
    "stop_mode": "signal_range",
    "stop_buffer_ticks": 1,
    "pending_expiry": "bars",
    "slippage_ticks": 1.0,
    "one_position_per_pair": True,
    "atr_period": 14,
    "minimum_stop_atr_fraction": 0.40,
    "maximum_stop_atr_fraction": 1.50,
    "minimum_spread_multiple": 3.0,
    "maximum_cost_to_risk_ratio": 0.10,
    "enforce_quality": True,
}


ALLOWED_PARAMETERS: dict[str, tuple[type, Any, Any]] = {
    "tolerance_ticks": (float, 0.0, 2.0),
    "trend_filter": (str, {"ema_slope", "none"}, None),
    "ema_length": (int, 10, 200),
    "ema_slope_lookback": (int, 1, 20),
    "use_session": (bool, None, None),
    "session_start": (str, None, None),
    "session_end": (str, None, None),
    "entry_model": (str, {"signal_close", "zone_reclaim", "origin_limit"}, None),
    "expiry_bars": (int, 1, 12),
    "origin_zone_atr_fraction": (float, 0.0, 0.30),
    "origin_zone_minimum_ticks": (int, 0, 5),
    "reclaim_buffer_ticks": (int, 0, 3),
    "minimum_body_ratio": (float, 0.70, 1.0),
    "maximum_wick_ticks": (float, 0.0, 2.0),
    "minimum_range_atr": (float, 0.40, 1.50),
    "maximum_range_atr": (float, 1.50, 3.0),
    "close_location_fraction": (float, 0.0, 0.20),
    "maximum_entry_displacement_atr": (float, 0.0, 0.40),
    "invalidate_on_trend_change": (bool, None, None),
}


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"Timestamp must include a timezone: {value}")
    return parsed.astimezone(UTC)


def _parse_clock(value: str) -> time:
    try:
        parsed = time.fromisoformat(value)
    except ValueError as exc:
        raise CandidateError(f"Invalid session time {value!r}; use HH:MM") from exc
    if parsed.tzinfo is not None or parsed.second or parsed.microsecond:
        raise CandidateError(f"Invalid session time {value!r}; use HH:MM")
    return parsed


def load_policy(path: Path = DEFAULT_POLICY) -> dict[str, Any]:
    policy = json.loads(path.read_text(encoding="utf-8"))
    if policy.get("schema_version") != 1:
        raise ValueError("Unsupported autoresearch policy schema")
    if policy.get("production_baseline_sha") != (
        "9ddd1b44aee10467247fda4627bc472ed1ff4132"
    ):
        raise ValueError("Policy is not pinned to the reviewed production baseline")
    return policy


def load_candidate(path: Path = DEFAULT_CANDIDATE) -> Candidate:
    source = path.read_text(encoding="utf-8")
    module = ast.parse(source, filename=str(path))
    assignments: list[ast.Assign] = []
    for index, node in enumerate(module.body):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            if index == 0 and isinstance(node.value.value, str):
                continue
        if isinstance(node, ast.Assign):
            assignments.append(node)
            continue
        raise CandidateError(
            "candidate.py may contain only a module docstring and CANDIDATE literal"
        )
    if len(assignments) != 1:
        raise CandidateError("candidate.py must assign CANDIDATE exactly once")
    assignment = assignments[0]
    if len(assignment.targets) != 1 or not isinstance(assignment.targets[0], ast.Name):
        raise CandidateError("CANDIDATE must be a direct assignment")
    if assignment.targets[0].id != "CANDIDATE":
        raise CandidateError("The only assignment must be named CANDIDATE")
    try:
        payload = ast.literal_eval(assignment.value)
    except (ValueError, SyntaxError) as exc:
        raise CandidateError("CANDIDATE must be composed only of Python literals") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "name",
        "description",
        "parameters",
    }:
        raise CandidateError(
            "CANDIDATE must contain exactly name, description, and parameters"
        )
    name = payload["name"]
    description = payload["description"]
    parameters = payload["parameters"]
    if not isinstance(name, str) or not NAME_PATTERN.fullmatch(name):
        raise CandidateError("Candidate name must be 2-64 lowercase slug characters")
    if not isinstance(description, str) or not 1 <= len(description) <= 240:
        raise CandidateError("Candidate description must contain 1-240 characters")
    if not isinstance(parameters, dict):
        raise CandidateError("Candidate parameters must be a dictionary")
    normalized = validate_parameters(parameters)
    return Candidate(
        name=name,
        description=description,
        parameters=normalized,
        source_sha256=hashlib.sha256(source.encode("utf-8")).hexdigest(),
    )


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate_parameters(parameters: dict[str, Any]) -> dict[str, Any]:
    unknown = sorted(set(parameters) - set(ALLOWED_PARAMETERS))
    if unknown:
        raise CandidateError(
            "Candidate attempted to change protected or unknown fields: "
            + ", ".join(unknown)
        )
    normalized: dict[str, Any] = {}
    for key, value in parameters.items():
        expected, lower_or_choices, upper = ALLOWED_PARAMETERS[key]
        if expected is bool:
            if not isinstance(value, bool):
                raise CandidateError(f"{key} must be a boolean")
        elif expected is str:
            if not isinstance(value, str):
                raise CandidateError(f"{key} must be a string")
            if isinstance(lower_or_choices, set) and value not in lower_or_choices:
                raise CandidateError(f"Unsupported {key}: {value}")
        elif expected is int:
            if not isinstance(value, int) or isinstance(value, bool):
                raise CandidateError(f"{key} must be an integer")
        elif expected is float:
            if not _is_number(value) or not math.isfinite(float(value)):
                raise CandidateError(f"{key} must be a finite number")
            value = float(value)
        if expected in {int, float}:
            if value < lower_or_choices or value > upper:
                raise CandidateError(
                    f"{key} must be between {lower_or_choices} and {upper}"
                )
        if key in {"session_start", "session_end"}:
            normalized[key] = _parse_clock(value)
        else:
            normalized[key] = value
    candidate_config = replace(NoWickConfig(), **normalized)
    if candidate_config.session_start >= candidate_config.session_end:
        raise CandidateError("session_start must be before session_end")
    for field, expected in IMMUTABLE_SAFETY.items():
        if getattr(candidate_config, field) != expected:
            raise CandidateError(f"Protected safety field changed: {field}")
    return normalized


def _read_m1(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with gzip.open(path, "rt", newline="", encoding="utf-8") as handle:
        for raw in csv.DictReader(handle):
            rows.append(
                {
                    "timestamp": _parse_utc(raw["timestamp_utc"]),
                    **{
                        key: float(raw[key])
                        for key in (
                            "bid_open",
                            "bid_high",
                            "bid_low",
                            "bid_close",
                            "ask_open",
                            "ask_high",
                            "ask_low",
                            "ask_close",
                        )
                    },
                }
            )
    return rows


def _resample_m15(
    rows: Iterable[dict[str, Any]],
) -> tuple[list[Bar], list[Bar], int]:
    buckets: dict[datetime, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        stamp = row["timestamp"]
        bucket = stamp.replace(minute=(stamp.minute // 15) * 15, second=0, microsecond=0)
        buckets[bucket].append(row)
    bids: list[Bar] = []
    asks: list[Bar] = []
    rejected = 0
    for stamp in sorted(buckets):
        group = sorted(buckets[stamp], key=lambda row: row["timestamp"])
        expected = [stamp + timedelta(minutes=offset) for offset in range(15)]
        if [row["timestamp"] for row in group] != expected:
            rejected += 1
            continue
        bids.append(
            Bar(
                stamp,
                group[0]["bid_open"],
                max(row["bid_high"] for row in group),
                min(row["bid_low"] for row in group),
                group[-1]["bid_close"],
            )
        )
        asks.append(
            Bar(
                stamp,
                group[0]["ask_open"],
                max(row["ask_high"] for row in group),
                min(row["ask_low"] for row in group),
                group[-1]["ask_close"],
            )
        )
    return bids, asks, rejected


def _resolve_source(directory: Path, instrument: str) -> Path:
    matches = sorted(directory.glob(f"{instrument.upper()}_M1_BIDASK_*.csv.gz"))
    if len(matches) != 1:
        raise FileNotFoundError(
            f"Expected one {instrument.upper()} M1 BID/ASK archive in {directory}; "
            f"found {len(matches)}"
        )
    return matches[0]


def _folds(policy: dict[str, Any]) -> list[Fold]:
    return [
        Fold(
            name=row["name"],
            directory=row["directory"],
            start=_parse_utc(row["start_utc"]),
            end=_parse_utc(row["end_utc_exclusive"]),
            minimum_trades=int(row["minimum_trades"]),
        )
        for row in policy["folds"]
    ]


def _trade_metrics(trades: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(
        trades,
        key=lambda row: (row["exit_time_utc"], row["pair"], row["order_id"]),
    )
    values = [float(row["net_r_after_costs"]) for row in ordered]
    winners = [value for value in values if value > 0]
    losers = [value for value in values if value < 0]
    equity = peak = drawdown = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    counts = Counter(row["pair"] for row in ordered)
    concentration = max(counts.values()) / len(ordered) if ordered else 1.0
    profit_factor = sum(winners) / -sum(losers) if losers else None
    return {
        "trades": len(values),
        "wins": len(winners),
        "losses": len(losers),
        "win_rate": len(winners) / len(values) if values else None,
        "net_r": sum(values),
        "expectancy_r": statistics.mean(values) if values else None,
        "profit_factor": profit_factor,
        "maximum_drawdown_r": drawdown,
        "distinct_pairs": len(counts),
        "maximum_pair_trade_share": concentration,
        "trades_by_pair": dict(sorted(counts.items())),
    }


def _load_fold_data(data_root: Path, fold: Fold) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    directory = data_root / fold.directory
    data: dict[str, Any] = {}
    qa: list[dict[str, Any]] = []
    for instrument in FOREX_MAJORS:
        source = _resolve_source(directory, instrument)
        bids, asks, incomplete = _resample_m15(_read_m1(source))
        if len(bids) != len(asks):
            raise RuntimeError(f"BID/ASK bar count mismatch for {instrument}")
        data[instrument] = (bids, asks)
        qa.append(
            {
                "pair": instrument.upper(),
                "source_file": source.name,
                "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "complete_m15_bars": len(bids),
                "incomplete_m15_buckets_rejected": incomplete,
                "first_bar_utc": bids[0].timestamp.isoformat(),
                "last_bar_utc": bids[-1].timestamp.isoformat(),
            }
        )
    return data, qa


def _json_parameters(parameters: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value.isoformat(timespec="minutes") if isinstance(value, time) else value
        for key, value in parameters.items()
    }


def evaluate(
    candidate: Candidate,
    *,
    data_root: Path,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    policy = policy or load_policy()
    fold_reports: dict[str, Any] = {}
    all_trades: list[dict[str, Any]] = []
    total_ambiguous = 0
    for fold in _folds(policy):
        data, qa = _load_fold_data(data_root, fold)
        fold_trades: list[dict[str, Any]] = []
        counters: Counter[str] = Counter()
        for instrument in FOREX_MAJORS:
            config = replace(
                NoWickConfig(instrument=instrument),
                **candidate.parameters,
            )
            for field, expected in IMMUTABLE_SAFETY.items():
                if getattr(config, field) != expected:
                    raise CandidateError(f"Protected safety field changed: {field}")
            bids, asks = data[instrument]
            result = run_no_wick_backtest(
                bids,
                ask_bars=asks,
                config=config,
                start=fold.start,
                end=fold.end,
            )
            fills = {fill.order_id: fill for fill in result.fills}
            for trade in result.trades:
                fill = fills[trade.order_id]
                fold_trades.append(
                    {
                        "fold": fold.name,
                        "pair": instrument.upper(),
                        **asdict(trade),
                        "risk_pips": fill.risk_pips,
                        "spread_pips": fill.spread / INSTRUMENTS[instrument].pip_size,
                        "cost_to_risk_ratio": fill.cost_to_risk_ratio,
                        "stop_distance_atr": fill.stop_distance_atr,
                        "body_ratio": fill.body_ratio,
                        "close_location": fill.close_location,
                        "range_atr": fill.wickless_range_atr,
                        "entry_displacement_atr": fill.entry_displacement_atr,
                    }
                )
            for field in (
                "eligible_signals",
                "pending_orders_created",
                "filled_orders",
                "expired_orders",
                "rejected_stop_too_tight",
                "rejected_stop_too_wide",
                "rejected_execution_cost",
                "rejected_wickless_quality",
                "rejected_no_origin_touch",
                "rejected_no_directional_reclaim",
                "rejected_entry_displacement",
                "invalidated_setups",
                "ambiguous_exits",
            ):
                counters[field] += int(getattr(result, field))
        fold_trades.sort(
            key=lambda row: (row["exit_time_utc"], row["pair"], row["order_id"])
        )
        total_ambiguous += counters["ambiguous_exits"]
        fold_reports[fold.name] = {
            "window": {
                "start_utc": fold.start.isoformat(),
                "end_utc_exclusive": fold.end.isoformat(),
            },
            "minimum_trades": fold.minimum_trades,
            "metrics": _trade_metrics(fold_trades),
            "counters": dict(sorted(counters.items())),
            "data_qa": qa,
        }
        all_trades.extend(fold_trades)
    overall = _trade_metrics(all_trades)
    overall["ambiguous_exits"] = total_ambiguous
    worst_fold_net = min(
        report["metrics"]["net_r"] for report in fold_reports.values()
    )
    comparable_pf = (
        overall["profit_factor"] if overall["profit_factor"] is not None else 1e12
    )
    objective = {
        "worst_fold_net_r": worst_fold_net,
        "total_net_r": overall["net_r"],
        "overall_profit_factor": comparable_pf,
        "negative_overall_drawdown_r": -overall["maximum_drawdown_r"],
        "total_trades": overall["trades"],
    }
    gates = acceptance_gates(
        fold_reports=fold_reports,
        overall=overall,
        policy=policy,
    )
    return {
        "schema_version": 1,
        "production_baseline_sha": policy["production_baseline_sha"],
        "candidate": {
            "name": candidate.name,
            "description": candidate.description,
            "parameters": _json_parameters(candidate.parameters),
            "source_sha256": candidate.source_sha256,
        },
        "safety": dict(IMMUTABLE_SAFETY),
        "folds": fold_reports,
        "overall": overall,
        "objective": objective,
        "acceptance_gates": gates,
        "trades": sorted(
            all_trades,
            key=lambda row: (
                row["exit_time_utc"],
                row["pair"],
                row["order_id"],
            ),
        ),
    }


def acceptance_gates(
    *,
    fold_reports: dict[str, Any],
    overall: dict[str, Any],
    policy: dict[str, Any],
) -> dict[str, Any]:
    acceptance = policy["acceptance"]
    checks: dict[str, bool] = {}
    for fold in _folds(policy):
        metrics = fold_reports[fold.name]["metrics"]
        checks[f"{fold.name}_minimum_trades"] = metrics["trades"] >= fold.minimum_trades
        checks[f"{fold.name}_positive_net_r"] = (
            metrics["net_r"] > acceptance["minimum_net_r_each_fold"]
        )
    checks.update(
        {
            "minimum_total_trades": overall["trades"]
            >= acceptance["minimum_total_trades"],
            "minimum_overall_profit_factor": (
                overall["profit_factor"] is None
                or overall["profit_factor"]
                >= acceptance["minimum_overall_profit_factor"]
            ),
            "maximum_overall_drawdown_r": overall["maximum_drawdown_r"]
            <= acceptance["maximum_overall_drawdown_r"],
            "maximum_pair_trade_share": overall["maximum_pair_trade_share"]
            <= acceptance["maximum_pair_trade_share"],
            "minimum_distinct_pairs": overall["distinct_pairs"]
            >= acceptance["minimum_distinct_pairs"],
            "maximum_ambiguous_exits": overall["ambiguous_exits"]
            <= acceptance["maximum_ambiguous_exits"],
        }
    )
    return {"passed": all(checks.values()), "checks": checks}


def objective_tuple(report: dict[str, Any], policy: dict[str, Any]) -> tuple[float, ...]:
    objective = report["objective"]
    return tuple(round(float(objective[key]), 8) for key in policy["objective_order"])


def candidate_beats(
    candidate_report: dict[str, Any],
    incumbent_report: dict[str, Any],
    policy: dict[str, Any],
) -> bool:
    if not candidate_report["acceptance_gates"]["passed"]:
        return False
    if not policy["acceptance"]["require_objective_improvement"]:
        return True
    return objective_tuple(candidate_report, policy) > objective_tuple(
        incumbent_report, policy
    )


def report_digest(report: dict[str, Any]) -> str:
    payload = json.dumps(report, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

