#!/usr/bin/env python3
"""Render and deliver durable autoresearch notifications."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from autoresearch.nightly_batch import discord_summary, post_discord


UTC = timezone.utc
LONDON = ZoneInfo("Europe/London")


def _metric(value: float) -> str:
    return f"{value:+.2f}R"


def _phase1_line(validation: dict[str, Any] | None) -> str | None:
    if not validation:
        return None
    diagnostics = validation.get("diagnostics", {})
    bootstrap = diagnostics.get("bootstrap", {})
    neighbourhood = validation.get("neighbourhood_robustness", {})
    fold_count = diagnostics.get("fold_count")
    profitable = diagnostics.get("profitable_fold_count")
    probability = bootstrap.get("probability_mean_positive")
    rolling = diagnostics.get("worst_rolling_three_fold_net_r")
    if None in {fold_count, profitable, probability, rolling}:
        return None
    neighbour_text = str(neighbourhood.get("status", "not available"))
    if neighbourhood.get("variant_count"):
        neighbour_text = (
            f"{neighbourhood.get('passing_variants', 0)}/"
            f"{neighbourhood['variant_count']} pass"
        )
    return (
        "Phase 1: "
        f"profitable {profitable}/{fold_count} | "
        f"P(mean monthly R > 0) {float(probability):.0%} | "
        f"worst 3-fold {float(rolling):+.2f}R | neighbours {neighbour_text}"
    )


def _insert_phase1_line(message: str, validation: dict[str, Any] | None) -> str:
    line = _phase1_line(validation)
    if line is None:
        return message
    rows = message.splitlines()
    index = next(
        (position for position, row in enumerate(rows) if row.startswith("Results:")),
        len(rows),
    )
    rows.insert(index, line)
    rendered = "\n".join(rows)
    return rendered if len(rendered) <= 2000 else message


def latest_nightly_summary(runs: Path) -> dict[str, Any]:
    paths = list(runs.glob("nightly-*.json"))
    if not paths:
        raise FileNotFoundError("No nightly summary was produced")
    latest = max(paths, key=lambda path: path.stat().st_mtime_ns)
    summary = json.loads(latest.read_text(encoding="utf-8"))
    incumbent_path = runs.parent / "incumbent.json"
    if incumbent_path.exists():
        incumbent = json.loads(incumbent_path.read_text(encoding="utf-8"))
        summary["_current_validation"] = incumbent.get("report", {}).get("validation")
    return summary


def bootstrap_message(summary: dict[str, Any]) -> str:
    selected = summary.get("selected")
    if selected is None:
        return "\n".join(
            (
                "⚠️ **Wickless bootstrap completed without a promoted benchmark**",
                f"Tested: {summary['tested']} | Passing all gates: 0",
                "The existing release-current reference remains active.",
            )
        )
    june = selected["folds"]["june_2026"]
    july = selected["folds"]["july_2026"]
    overall = selected["overall"]
    lines = [
        "✅ **Wickless bootstrap benchmark promoted**",
        f"Source: **{selected['source_candidate']}**",
        f"Tested: {summary['tested']} | Passing all gates: {summary['passing']}",
        f"June: {june['trades']} trades, {_metric(june['net_r'])}",
        f"July: {july['trades']} trades, {_metric(july['net_r'])}",
        (
            f"Overall: {overall['trades']} trades, {_metric(overall['net_r'])}, "
            f"drawdown {overall['maximum_drawdown_r']:.2f}R"
        ),
    ]
    phase1 = _phase1_line(selected.get("validation"))
    if phase1:
        lines.append(phase1)
    return "\n".join(lines)


def refresh_message(result: dict[str, Any]) -> str:
    june = result["folds"]["june_2026"]
    july = result["folds"]["july_2026"]
    overall = result["overall"]
    release = str(result.get("production_release_sha", "unknown"))[:8]
    now = datetime.now(UTC).replace(microsecond=0)
    lines = [
        "📌 **Wickless production reference refreshed**",
        f"Release: `{release}`",
        f"UTC: `{now.isoformat()}`",
        f"London: `{now.astimezone(LONDON).isoformat()}`",
        f"June: {june['trades']} trades, {_metric(june['net_r'])}",
        f"July: {july['trades']} trades, {_metric(july['net_r'])}",
        (
            f"Overall: {overall['trades']} trades, {_metric(overall['net_r'])}, "
            f"drawdown {overall['maximum_drawdown_r']:.2f}R"
        ),
    ]
    phase1 = _phase1_line(result.get("validation"))
    if phase1:
        lines.append(phase1)
    lines.append("Candidate acceptance gates remain unchanged or stricter.")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("nightly", "bootstrap", "refresh", "failure"))
    parser.add_argument("--path", type=Path)
    parser.add_argument("--run-url")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.mode == "nightly":
        if args.path is None:
            raise ValueError("nightly mode requires --path to the runs directory")
        summary = latest_nightly_summary(args.path)
        validation = (
            (summary.get("best_experiment") or {}).get("validation")
            or summary.get("_current_validation")
        )
        message = _insert_phase1_line(discord_summary(summary), validation)
    elif args.mode == "bootstrap":
        if args.path is None:
            raise ValueError("bootstrap mode requires --path")
        message = bootstrap_message(json.loads(args.path.read_text(encoding="utf-8")))
    elif args.mode == "refresh":
        if args.path is None:
            raise ValueError("refresh mode requires --path")
        message = refresh_message(json.loads(args.path.read_text(encoding="utf-8")))
    else:
        message = "❌ **Wickless autoresearch failed**"
        if args.run_url:
            message += f"\n{args.run_url}"

    print(message)
    if not post_discord(message):
        raise SystemExit("DISCORD_WEBHOOK_URL is not configured")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
