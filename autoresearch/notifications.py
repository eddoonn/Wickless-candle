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


def latest_nightly_summary(runs: Path) -> dict[str, Any]:
    paths = list(runs.glob("nightly-*.json"))
    if not paths:
        raise FileNotFoundError("No nightly summary was produced")
    latest = max(paths, key=lambda path: path.stat().st_mtime_ns)
    return json.loads(latest.read_text(encoding="utf-8"))


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
    return "\n".join(
        (
            "✅ **Wickless bootstrap benchmark promoted**",
            f"Source: **{selected['source_candidate']}**",
            f"Tested: {summary['tested']} | Passing all gates: {summary['passing']}",
            f"June: {june['trades']} trades, {_metric(june['net_r'])}",
            f"July: {july['trades']} trades, {_metric(july['net_r'])}",
            (
                f"Overall: {overall['trades']} trades, {_metric(overall['net_r'])}, "
                f"drawdown {overall['maximum_drawdown_r']:.2f}R"
            ),
        )
    )


def refresh_message(result: dict[str, Any]) -> str:
    june = result["folds"]["june_2026"]
    july = result["folds"]["july_2026"]
    overall = result["overall"]
    release = str(result.get("production_release_sha", "unknown"))[:8]
    now = datetime.now(UTC).replace(microsecond=0)
    return "\n".join(
        (
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
            "Candidate acceptance gates remain unchanged.",
        )
    )


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
        message = discord_summary(latest_nightly_summary(args.path))
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
