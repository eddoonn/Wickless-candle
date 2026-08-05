"""Stable fingerprints for comparing research behavior and trade outcomes."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def outcome_payload(report: dict[str, Any]) -> dict[str, Any]:
    """Return only realized trade outcomes and metrics."""

    return {
        "folds": {
            name: value["metrics"]
            for name, value in sorted(report["folds"].items())
        },
        "overall": report["overall"],
        "trades": report.get("trades", []),
    }


def behavior_payload(report: dict[str, Any]) -> dict[str, Any]:
    """Return outcomes plus signal-funnel counters, excluding candidate identity."""

    return {
        **outcome_payload(report),
        "counters": {
            name: value.get("counters", {})
            for name, value in sorted(report["folds"].items())
        },
    }


def _digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def outcome_digest(report: dict[str, Any]) -> str:
    return _digest(outcome_payload(report))


def behavior_digest(report: dict[str, Any]) -> str:
    return _digest(behavior_payload(report))
