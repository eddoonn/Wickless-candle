#!/usr/bin/env python3
"""Compare production defaults across alternative timezone-aware entry sessions."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import no_wick_research as engine
from autoresearch.evaluator import Candidate, evaluate, load_policy
from time_display import london_iso


UTC = timezone.utc
LONDON = ZoneInfo("Europe/London")
NEW_YORK = ZoneInfo("America/New_York")
LONDON_START = time(8, 0)
LONDON_END = time(17, 0)
SessionPredicate = Callable[[engine.Bar, engine.NoWickConfig], bool]


def _clock(bar: engine.Bar, zone: ZoneInfo) -> time:
    return bar.timestamp.astimezone(zone).time().replace(tzinfo=None)


def current_new_york_session(bar: engine.Bar, config: engine.NoWickConfig) -> bool:
    """Use the existing 05:00-13:30 America/New_York production window."""

    if not config.use_session:
        return True
    local_open = _clock(bar, NEW_YORK)
    return config.session_start <= local_open < config.session_end


def full_london_session(bar: engine.Bar, config: engine.NoWickConfig) -> bool:
    """Use the full 08:00-17:00 Europe/London session."""

    if not config.use_session:
        return True
    local_open = _clock(bar, LONDON)
    return LONDON_START <= local_open < LONDON_END


def london_new_york_union(bar: engine.Bar, config: engine.NoWickConfig) -> bool:
    """Accept the full London session plus the existing New York window."""

    return full_london_session(bar, config) or current_new_york_session(bar, config)


def all_hours(_bar: engine.Bar, _config: engine.NoWickConfig) -> bool:
    """Diagnostic only: disable the entry-session filter."""

    return True


MODES: dict[str, tuple[str, SessionPredicate]] = {
    "current-new-york": (
        "Existing 05:00-13:30 America/New_York window",
        current_new_york_session,
    ),
    "full-london": (
        "Full 08:00-17:00 Europe/London window",
        full_london_session,
    ),
    "london-new-york-union": (
        "08:00-17:00 Europe/London OR 05:00-13:30 America/New_York",
        london_new_york_union,
    ),
    "all-hours-diagnostic": (
        "Twenty-four-hour diagnostic without an entry-session filter",
        all_hours,
    ),
}


def _candidate(mode: str, description: str) -> Candidate:
    digest = hashlib.sha256(f"production-defaults|{mode}".encode("utf-8")).hexdigest()
    return Candidate(
        name=f"session-{mode}",
        description=description,
        parameters={},
        source_sha256=digest,
    )


def evaluate_mode(
    mode: str,
    *,
    data_root: Path,
    policy: dict[str, Any],
) -> dict[str, Any]:
    description, predicate = MODES[mode]
    original = engine._in_entry_session
    engine._in_entry_session = predicate
    try:
        return evaluate(
            _candidate(mode, description),
            data_root=data_root,
            policy=policy,
        )
    finally:
        engine._in_entry_session = original


def compact(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "candidate": report["candidate"],
        "folds": {
            name: {
                "window": value["window"],
                "metrics": value["metrics"],
                "counters": value["counters"],
            }
            for name, value in report["folds"].items()
        },
        "overall": report["overall"],
        "objective": report["objective"],
        "acceptance_gates": report["acceptance_gates"],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument(
        "--policy", type=Path, default=Path("autoresearch/policy.json")
    )
    parser.add_argument(
        "--output", type=Path, default=Path("reports/session-comparison/latest")
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    policy = load_policy(args.policy)
    args.output.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.now(UTC).replace(microsecond=0)
    generated_at_utc = generated_at.isoformat()
    results: dict[str, Any] = {}
    for mode in MODES:
        report = evaluate_mode(mode, data_root=args.data_root, policy=policy)
        results[mode] = compact(report)
        (args.output / f"{mode}.json").write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        metrics = report["overall"]
        print(
            json.dumps(
                {
                    "mode": mode,
                    "june_trades": report["folds"]["june_2026"]["metrics"]["trades"],
                    "july_trades": report["folds"]["july_2026"]["metrics"]["trades"],
                    "total_trades": metrics["trades"],
                    "net_r": metrics["net_r"],
                    "passed": report["acceptance_gates"]["passed"],
                },
                sort_keys=True,
            ),
            flush=True,
        )
    summary = {
        "schema_version": 1,
        "generated_at_utc": generated_at_utc,
        "generated_at_london": london_iso(generated_at_utc),
        "production_parameters_changed": False,
        "comparison_only": True,
        "london_session": "08:00-17:00 Europe/London",
        "new_york_session": "05:00-13:30 America/New_York",
        "results": results,
    }
    (args.output / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
