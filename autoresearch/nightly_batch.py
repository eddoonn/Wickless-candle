#!/usr/bin/env python3
"""Run a deterministic, profitability-first batch of Wickless experiments."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import subprocess
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from pprint import pformat
from typing import Any
from zoneinfo import ZoneInfo

from autoresearch.evaluator import load_candidate
from autoresearch.run_experiment import _read_ledger, main as run_experiment


UTC = timezone.utc
LONDON = ZoneInfo("Europe/London")
HERE = Path(__file__).resolve().parent
REPOSITORY_URL = "https://github.com/eddoonn/Wickless-candle"
NIGHTLY_BRANCH = "autoresearch/nightly"


@dataclass(frozen=True)
class Proposal:
    name: str
    description: str
    parameters: dict[str, Any]


# Small changes around the reviewed production defaults. Safety and execution
# parameters are intentionally absent because evaluator.py does not expose them.
SEARCH_VALUES: tuple[tuple[str, tuple[Any, ...]], ...] = (
    ("minimum_body_ratio", (0.82, 0.81, 0.79, 0.78, 0.84, 0.76)),
    ("minimum_range_atr", (0.52, 0.48, 0.54, 0.46, 0.56, 0.44)),
    ("maximum_range_atr", (1.90, 2.10, 1.80, 2.20)),
    ("close_location_fraction", (0.09, 0.11, 0.08, 0.12)),
    ("ema_length", (45, 55, 40, 60, 35, 65)),
    ("ema_slope_lookback", (4, 6, 3, 7, 2, 8)),
    ("session_start", ("04:45", "05:15", "04:30", "05:30")),
    ("session_end", ("13:15", "13:45", "13:00", "14:00")),
    ("maximum_entry_displacement_atr", (0.28, 0.32, 0.26, 0.34)),
    ("tolerance_ticks", (1.5, 1.0, 0.5)),
    ("maximum_wick_ticks", (1.75, 1.50, 1.25)),
)


def parameter_signature(parameters: dict[str, Any]) -> str:
    return json.dumps(parameters, sort_keys=True, separators=(",", ":"))


def proposal_space() -> list[Proposal]:
    """Return stable single-factor tests followed by two-factor neighbours."""

    raw: list[dict[str, Any]] = []
    for key, values in SEARCH_VALUES:
        raw.extend({key: value} for value in values)
    for left_index, (left_key, left_values) in enumerate(SEARCH_VALUES):
        for right_key, right_values in SEARCH_VALUES[left_index + 1 :]:
            for left_value in left_values:
                for right_value in right_values:
                    raw.append(
                        {left_key: left_value, right_key: right_value}
                    )
    proposals: list[Proposal] = []
    for index, parameters in enumerate(raw, 1):
        detail = ", ".join(f"{key}={value}" for key, value in parameters.items())
        proposals.append(
            Proposal(
                name=f"grid-{index:04d}",
                description=f"Controlled test of {detail} against production defaults.",
                parameters=parameters,
            )
        )
    return proposals


def tested_signatures(ledger: Path) -> set[str]:
    records, _ = _read_ledger(ledger)
    return {
        parameter_signature(record["candidate"]["parameters"])
        for record in records
    }


def select_proposals(ledger: Path, batch_size: int) -> list[Proposal]:
    tested = tested_signatures(ledger)
    return [
        proposal
        for proposal in proposal_space()
        if parameter_signature(proposal.parameters) not in tested
    ][:batch_size]


def render_candidate(proposal: Proposal) -> str:
    payload = {
        "name": proposal.name,
        "description": proposal.description,
        "parameters": proposal.parameters,
    }
    return (
        '"""The only strategy file an autoresearch agent may edit."""\n\n'
        f"CANDIDATE = {pformat(payload, sort_dicts=False, width=88)}\n"
    )


def write_candidate(path: Path, proposal: Proposal) -> None:
    path.write_text(render_candidate(proposal), encoding="utf-8")
    load_candidate(path)


def git_commit(message: str, paths: list[Path]) -> str:
    root = HERE.parent
    relative = [str(path.relative_to(root)) for path in paths if path.exists()]
    if relative:
        subprocess.run(["git", "add", "--", *relative], cwd=root, check=True)
    changed = subprocess.run(
        ["git", "diff", "--cached", "--quiet"], cwd=root
    ).returncode
    if changed:
        subprocess.run(["git", "commit", "-m", message], cwd=root, check=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def post_discord(message: str, webhook: str | None = None) -> bool:
    target = webhook or os.getenv("DISCORD_WEBHOOK_URL")
    if not target:
        return False
    request = urllib.request.Request(
        target,
        data=json.dumps({"content": message}).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "wickless-autoresearch/1"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        if response.status not in {200, 204}:
            raise RuntimeError(f"Discord returned HTTP {response.status}")
    return True


def _metric(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:+.2f}R"


def discord_summary(summary: dict[str, Any]) -> str:
    incumbent = summary["incumbent"]
    folds = incumbent["folds"]
    return "\n".join(
        (
            f"🔬 **Wickless nightly autoresearch — {summary['london_date']}**",
            (
                f"Tested **{summary['tested']}** | KEEP **{summary['kept']}** | "
                f"REJECT **{summary['rejected']}**"
            ),
            f"Best candidate: **{incumbent['name']}**",
            (
                f"June: {folds['june_2026']['trades']} trades, "
                f"{_metric(folds['june_2026']['net_r'])}"
            ),
            (
                f"July: {folds['july_2026']['trades']} trades, "
                f"{_metric(folds['july_2026']['net_r'])}"
            ),
            (
                f"Overall: {incumbent['overall']['trades']} trades, "
                f"{_metric(incumbent['overall']['net_r'])}, "
                f"drawdown {incumbent['overall']['maximum_drawdown_r']:.2f}R"
            ),
            f"Results: {REPOSITORY_URL}/tree/{NIGHTLY_BRANCH}",
        )
    )


def keep_message(output: dict[str, Any]) -> str:
    folds = output["folds"]
    overall = output["overall"]
    return "\n".join(
        (
            "✅ **Strong Wickless candidate found**",
            f"Candidate: **{output['candidate']}**",
            f"June: {folds['june_2026']['trades']} trades, {_metric(folds['june_2026']['net_r'])}",
            f"July: {folds['july_2026']['trades']} trades, {_metric(folds['july_2026']['net_r'])}",
            f"Overall: {overall['trades']} trades, {_metric(overall['net_r'])}",
            "It remains isolated from the live strategy pending review.",
        )
    )


def _incumbent_summary(path: Path) -> dict[str, Any]:
    report = json.loads(path.read_text(encoding="utf-8"))["report"]
    return {
        "name": report["candidate"]["name"],
        "description": report["candidate"]["description"],
        "parameters": report["candidate"]["parameters"],
        "overall": report["overall"],
        "folds": {
            name: value["metrics"] for name, value in report["folds"].items()
        },
        "acceptance_gates": report["acceptance_gates"],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument("--batch-size", type=int, default=12)
    parser.add_argument("--candidate", type=Path, default=HERE / "candidate.py")
    parser.add_argument("--policy", type=Path, default=HERE / "policy.json")
    parser.add_argument("--ledger", type=Path, default=HERE / "results.jsonl")
    parser.add_argument("--incumbent", type=Path, default=HERE / "incumbent.json")
    parser.add_argument("--runs", type=Path, default=HERE / "runs")
    parser.add_argument("--git-commits", action="store_true")
    parser.add_argument("--no-discord", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not 1 <= args.batch_size <= 20:
        raise ValueError("--batch-size must be between 1 and 20")
    selected = select_proposals(args.ledger, args.batch_size)
    outputs: list[dict[str, Any]] = []
    for proposal in selected:
        write_candidate(args.candidate, proposal)
        commit = None
        if args.git_commits:
            commit = git_commit(
                f"autoresearch: propose {proposal.name}", [args.candidate]
            )
        command = [
            "--data-root",
            str(args.data_root),
            "--candidate",
            str(args.candidate),
            "--policy",
            str(args.policy),
            "--ledger",
            str(args.ledger),
            "--incumbent",
            str(args.incumbent),
            "--runs",
            str(args.runs),
        ]
        if commit:
            command.extend(("--commit", commit))
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            code = run_experiment(command)
        output = json.loads(captured.getvalue())
        outputs.append(output)
        if args.git_commits:
            git_commit(
                f"autoresearch: record {proposal.name} {output['status']}",
                [args.ledger, args.incumbent, args.runs],
            )
        if code == 0 and not args.no_discord:
            post_discord(keep_message(output))

    incumbent = _incumbent_summary(args.incumbent)
    incumbent_proposal = Proposal(
        name=incumbent["name"],
        description=incumbent["description"],
        parameters=incumbent["parameters"],
    )
    write_candidate(args.candidate, incumbent_proposal)
    generated = datetime.now(UTC).replace(microsecond=0)
    summary = {
        "schema_version": 1,
        "generated_at_utc": generated.isoformat(),
        "london_date": generated.astimezone(LONDON).date().isoformat(),
        "tested": len(outputs),
        "kept": sum(output["status"] == "keep" for output in outputs),
        "rejected": sum(output["status"] == "discard" for output in outputs),
        "experiments": outputs,
        "incumbent": incumbent,
    }
    args.runs.mkdir(parents=True, exist_ok=True)
    summary_path = args.runs / f"nightly-{generated:%Y%m%dT%H%M%SZ}.json"
    summary_path.write_text(
        json.dumps(summary, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    if args.git_commits:
        git_commit(
            f"autoresearch: complete nightly batch {summary['london_date']}",
            [args.candidate, summary_path],
        )
    if not args.no_discord:
        post_discord(discord_summary(summary))
    print(json.dumps(summary, indent=2, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
