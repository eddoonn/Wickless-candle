"""Append-only worker attempt log and idea-category helpers."""

from __future__ import annotations

import csv
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


OBJECTIVE_KEYS = (
    "worst_fold_net_r",
    "total_net_r",
    "overall_profit_factor",
    "negative_overall_drawdown_r",
    "total_trades",
)

CATEGORY_DIRECTIONS: dict[str, str] = {
    "candle-quality": (
        "Test candle body, range, and close-location filters as one coherent quality idea."
    ),
    "trend-filter": (
        "Test EMA horizon, slope confirmation, or disabling the trend filter."
    ),
    "entry-model": (
        "Test coherent reclaim or origin-limit entry geometry without changing risk rules."
    ),
    "wick-detection": (
        "Test wick and rounding tolerance while preserving the opening-side wickless rule."
    ),
}

# Historical rows remain readable, but locked production sessions are no longer
# an active research category and cannot be selected by the coach.
LEGACY_CATEGORIES = frozenset({"session-window"})

PARAMETER_CATEGORIES = {
    "minimum_body_ratio": "candle-quality",
    "minimum_range_atr": "candle-quality",
    "maximum_range_atr": "candle-quality",
    "close_location_fraction": "candle-quality",
    "ema_length": "trend-filter",
    "ema_slope_lookback": "trend-filter",
    "trend_filter": "trend-filter",
    "entry_model": "entry-model",
    "expiry_bars": "entry-model",
    "origin_zone_atr_fraction": "entry-model",
    "origin_zone_minimum_ticks": "entry-model",
    "reclaim_buffer_ticks": "entry-model",
    "maximum_entry_displacement_atr": "entry-model",
    "invalidate_on_trend_change": "entry-model",
    "tolerance_ticks": "wick-detection",
    "maximum_wick_ticks": "wick-detection",
}


@dataclass(frozen=True)
class Attempt:
    timestamp: str
    description: str
    category: str
    score: str
    decision: str

    @property
    def kept(self) -> bool:
        return self.decision == "KEPT"


def _category_is_valid(category: str) -> bool:
    if category in {"baseline", "uncategorized"}:
        return True
    allowed = set(CATEGORY_DIRECTIONS) | set(LEGACY_CATEGORIES)
    return all(part in allowed for part in category.split("+"))


def parameter_categories(parameters: dict[str, Any]) -> tuple[str, ...]:
    """Return stable high-level categories represented by candidate parameters."""

    if not parameters:
        return ("baseline",)
    categories = {
        PARAMETER_CATEGORIES.get(parameter, "uncategorized")
        for parameter in parameters
    }
    order = {name: index for index, name in enumerate(CATEGORY_DIRECTIONS)}
    return tuple(sorted(categories, key=lambda name: (order.get(name, 999), name)))


def idea_category(parameters: dict[str, Any]) -> str:
    return "+".join(parameter_categories(parameters))


def one_sentence(value: str) -> str:
    normalized = " ".join(value.split()).strip()
    if not normalized:
        return "No description supplied."
    match = re.match(r"^(.+?[.!?])(?:\s|$)", normalized)
    if match:
        return match.group(1)
    return normalized.rstrip(".!?") + "."


def format_score(objective: dict[str, Any]) -> str:
    """Serialize the unchanged lexicographic objective into one log field."""

    parts: list[str] = []
    for key in OBJECTIVE_KEYS:
        value = objective[key]
        if isinstance(value, int):
            rendered = str(value)
        else:
            rendered = f"{float(value):.8g}"
        parts.append(f"{key}={rendered}")
    return ";".join(parts)


def parse_score(value: str) -> tuple[float, ...]:
    parsed: dict[str, float] = {}
    for item in value.split(";"):
        key, separator, raw = item.partition("=")
        if not separator:
            raise ValueError(f"Invalid score field: {value}")
        parsed[key] = float(raw)
    missing = [key for key in OBJECTIVE_KEYS if key not in parsed]
    if missing:
        raise ValueError("Score is missing objective fields: " + ", ".join(missing))
    return tuple(parsed[key] for key in OBJECTIVE_KEYS)


def append_attempt(
    path: Path,
    *,
    timestamp: str,
    description: str,
    category: str,
    score: str,
    status: str,
) -> Attempt:
    """Append one CSV line; existing bytes are never rewritten or truncated."""

    decision = {"keep": "KEPT", "discard": "DISCARDED"}.get(status)
    if decision is None:
        raise ValueError(f"Unsupported attempt status: {status}")
    datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    if not _category_is_valid(category):
        raise ValueError(f"Unsupported attempt category: {category}")
    parse_score(score)
    attempt = Attempt(
        timestamp=timestamp,
        description=one_sentence(description),
        category=category,
        score=score,
        decision=decision,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="") as handle:
        csv.writer(handle, lineterminator="\n").writerow(
            (
                attempt.timestamp,
                attempt.description,
                attempt.category,
                attempt.score,
                attempt.decision,
            )
        )
        handle.flush()
        os.fsync(handle.fileno())
    return attempt


def read_attempts(path: Path) -> list[Attempt]:
    if not path.exists():
        return []
    attempts: list[Attempt] = []
    with path.open("r", encoding="utf-8", newline="") as handle:
        for line_number, row in enumerate(csv.reader(handle), 1):
            if not row:
                continue
            if len(row) != 5:
                raise ValueError(
                    f"attempts.log line {line_number} must contain exactly five fields"
                )
            timestamp, description, category, score, decision = row
            datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            if not _category_is_valid(category):
                raise ValueError(
                    f"attempts.log line {line_number} has invalid category {category}"
                )
            parse_score(score)
            if decision not in {"KEPT", "DISCARDED"}:
                raise ValueError(
                    f"attempts.log line {line_number} has invalid decision {decision}"
                )
            attempts.append(
                Attempt(timestamp, description, category, score, decision)
            )
    return attempts


def attempt_from_ledger_record(record: dict[str, Any]) -> Attempt:
    candidate = record["candidate"]
    status = record["status"]
    decision = {"keep": "KEPT", "discard": "DISCARDED"}.get(status)
    if decision is None:
        raise ValueError(f"Unsupported ledger status: {status}")
    category = record.get("category") or idea_category(candidate["parameters"])
    score = record.get("score") or format_score(record["objective"])
    parse_score(score)
    return Attempt(
        timestamp=record["generated_at_utc"],
        description=one_sentence(candidate["description"]),
        category=category,
        score=score,
        decision=decision,
    )


def sync_attempts_from_ledger(
    path: Path,
    records: list[dict[str, Any]],
) -> int:
    """Reconcile the readable log with the hash-chained source of truth."""

    existing = read_attempts(path)
    if len(existing) > len(records):
        raise ValueError("attempts.log contains more rows than results.jsonl")
    expected = [attempt_from_ledger_record(record) for record in records]
    for index, attempt in enumerate(existing):
        if attempt != expected[index]:
            raise ValueError(
                f"attempts.log line {index + 1} does not match the result ledger"
            )
    for attempt in expected[len(existing) :]:
        append_attempt(
            path,
            timestamp=attempt.timestamp,
            description=attempt.description,
            category=attempt.category,
            score=attempt.score,
            status="keep" if attempt.kept else "discard",
        )
    return len(expected) - len(existing)
