#!/usr/bin/env python3
"""Re-run the reviewed production strategy and write UTC/London reports."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from autoresearch.evaluator import Candidate, FOREX_MAJORS, evaluate, load_policy
from time_display import london_iso


UTC = timezone.utc
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "reports" / "production-backtest" / "latest"


def _candidate(production_sha: str) -> Candidate:
    identity = f"production-baseline:{production_sha}"
    return Candidate(
        name="production-baseline",
        description=f"Reviewed Wickless production defaults at {production_sha[:8]}.",
        parameters={},
        source_sha256=hashlib.sha256(identity.encode("utf-8")).hexdigest(),
    )


def _add_london(mapping: dict[str, Any], utc_key: str, london_key: str) -> None:
    value = mapping.get(utc_key)
    if isinstance(value, str) and value:
        mapping[london_key] = london_iso(value)


def enrich_report_times(
    report: dict[str, Any],
    *,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Return a report whose windows, data QA and trades pair UTC with London."""

    enriched = deepcopy(report)
    generated = (generated_at or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
    enriched["generated_at_utc"] = generated.isoformat()
    enriched["generated_at_london"] = generated.astimezone(
        __import__("zoneinfo").ZoneInfo("Europe/London")
    ).isoformat()
    enriched["display_timezone"] = "Europe/London"

    for fold in enriched.get("folds", {}).values():
        window = fold.get("window", {})
        _add_london(window, "start_utc", "start_london")
        _add_london(window, "end_utc_exclusive", "end_london_exclusive")
        for row in fold.get("data_qa", []):
            _add_london(row, "first_bar_utc", "first_bar_london")
            _add_london(row, "last_bar_utc", "last_bar_london")

    for trade in enriched.get("trades", []):
        for utc_key in ("signal_time_utc", "entry_time_utc", "exit_time_utc"):
            _add_london(trade, utc_key, utc_key.removesuffix("_utc") + "_london")
    return enriched


def compact_summary(report: dict[str, Any]) -> dict[str, Any]:
    """Return the stable human-facing subset used by Actions and Discord."""

    return {
        "schema_version": 1,
        "generated_at_utc": report["generated_at_utc"],
        "generated_at_london": report["generated_at_london"],
        "display_timezone": report["display_timezone"],
        "strategy": report["candidate"],
        "production_baseline_sha": report["production_baseline_sha"],
        "universe": [instrument.upper() for instrument in FOREX_MAJORS],
        "folds": {
            name: {
                "window": fold["window"],
                "minimum_trades": fold["minimum_trades"],
                "metrics": fold["metrics"],
                "counters": fold["counters"],
            }
            for name, fold in report["folds"].items()
        },
        "overall": report["overall"],
        "objective": report["objective"],
        "acceptance_gates": report["acceptance_gates"],
    }


def _trade_fieldnames(trades: list[dict[str, Any]]) -> list[str]:
    preferred = [
        "fold",
        "pair",
        "order_id",
        "side",
        "pattern",
        "signal_time_utc",
        "signal_time_london",
        "entry_time_utc",
        "entry_time_london",
        "exit_time_utc",
        "exit_time_london",
        "entry",
        "stop",
        "target",
        "exit",
        "exit_reason",
        "gross_r",
        "net_r_after_costs",
    ]
    available = {key for trade in trades for key in trade}
    ordered = [key for key in preferred if key in available]
    ordered.extend(sorted(available - set(ordered)))
    return ordered


def write_report(report: dict[str, Any], output: Path) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=True)
    summary = compact_summary(report)
    (output / "report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    (output / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    trades = report.get("trades", [])
    fieldnames = _trade_fieldnames(trades)
    with (output / "trades.csv").open("w", newline="", encoding="utf-8") as handle:
        if fieldnames:
            writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(trades)

    history = output.parent / "history"
    history.mkdir(parents=True, exist_ok=True)
    stamp = report["generated_at_utc"].replace(":", "").replace("-", "")
    (history / f"{stamp}-summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument(
        "--policy",
        type=Path,
        default=ROOT / "autoresearch" / "policy.json",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    policy = load_policy(args.policy)
    report = evaluate(
        _candidate(policy["production_baseline_sha"]),
        data_root=args.data_root,
        policy=policy,
    )
    enriched = enrich_report_times(report)
    summary = write_report(enriched, args.output)
    print(json.dumps(summary, indent=2, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
