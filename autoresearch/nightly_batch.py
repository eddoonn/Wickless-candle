#!/usr/bin/env python3
"""Run a deterministic, diversified, profitability-first experiment batch."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import subprocess
import time as time_module
import urllib.error
import urllib.request
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from pprint import pformat
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from autoresearch.attempts import idea_category, parameter_categories, read_attempts
from autoresearch.behavior import behavior_digest
from autoresearch.coach import playbook_guidance, run_coach_if_due
from autoresearch.evaluator import load_candidate, load_policy, objective_tuple
from autoresearch.run_experiment import _read_ledger, main as run_experiment


UTC = timezone.utc
LONDON = ZoneInfo("Europe/London")
HERE = Path(__file__).resolve().parent
REPOSITORY_URL = "https://github.com/eddoonn/Wickless-candle"
NIGHTLY_BRANCH = "autoresearch/nightly"
LOCKED_SESSION_PARAMETERS = frozenset({"use_session", "session_start", "session_end"})


@dataclass(frozen=True)
class Proposal:
    name: str
    description: str
    parameters: dict[str, Any]


# Values intentionally exclude the reviewed defaults. London and New York clocks
# are fixed production policy and therefore never appear on the candidate surface.
SEARCH_VALUES: tuple[tuple[str, tuple[Any, ...]], ...] = (
    ("minimum_body_ratio", (0.82, 0.78, 0.84, 0.76, 0.86, 0.74)),
    ("minimum_range_atr", (0.52, 0.48, 0.56, 0.44, 0.60, 0.40)),
    ("maximum_range_atr", (1.90, 2.10, 1.80, 2.20)),
    ("close_location_fraction", (0.09, 0.11, 0.08, 0.12)),
    ("ema_length", (45, 55, 40, 60, 35, 65)),
    ("ema_slope_lookback", (4, 6, 3, 7, 2, 8)),
    ("trend_filter", ("none",)),
    ("tolerance_ticks", (1.5, 1.0, 0.5)),
    ("maximum_wick_ticks", (1.75, 1.50, 1.25)),
)


# Alternative entry models need coherent supporting parameters. Keeping these as
# curated bundles avoids spending runs on settings that cannot affect signal-close.
CURATED_ENTRY_PROPOSALS: tuple[dict[str, Any], ...] = (
    {
        "entry_model": "zone_reclaim",
        "expiry_bars": 2,
        "origin_zone_atr_fraction": 0.08,
        "reclaim_buffer_ticks": 0,
        "maximum_entry_displacement_atr": 0.20,
    },
    {
        "entry_model": "zone_reclaim",
        "expiry_bars": 4,
        "origin_zone_atr_fraction": 0.12,
        "reclaim_buffer_ticks": 1,
        "maximum_entry_displacement_atr": 0.30,
    },
    {
        "entry_model": "zone_reclaim",
        "expiry_bars": 6,
        "origin_zone_atr_fraction": 0.16,
        "reclaim_buffer_ticks": 1,
        "maximum_entry_displacement_atr": 0.34,
    },
    {
        "entry_model": "zone_reclaim",
        "expiry_bars": 3,
        "origin_zone_atr_fraction": 0.10,
        "reclaim_buffer_ticks": 2,
        "invalidate_on_trend_change": False,
    },
    {
        "entry_model": "origin_limit",
        "expiry_bars": 2,
        "origin_zone_atr_fraction": 0.08,
        "origin_zone_minimum_ticks": 1,
    },
    {
        "entry_model": "origin_limit",
        "expiry_bars": 4,
        "origin_zone_atr_fraction": 0.12,
        "origin_zone_minimum_ticks": 2,
    },
    {
        "entry_model": "origin_limit",
        "expiry_bars": 6,
        "origin_zone_atr_fraction": 0.16,
        "origin_zone_minimum_ticks": 2,
    },
    {
        "entry_model": "origin_limit",
        "expiry_bars": 3,
        "origin_zone_atr_fraction": 0.10,
        "origin_zone_minimum_ticks": 3,
        "invalidate_on_trend_change": False,
    },
)


def parameter_signature(parameters: dict[str, Any]) -> str:
    return json.dumps(parameters, sort_keys=True, separators=(",", ":"))


def proposal_family(parameters: dict[str, Any]) -> str:
    return "+".join(sorted(parameters))


def proposal_space() -> list[Proposal]:
    """Return stable meaningful single, paired, and curated entry-model tests."""

    raw: list[dict[str, Any]] = []
    for key, values in SEARCH_VALUES:
        raw.extend({key: value} for value in values)
    for left_index, (left_key, left_values) in enumerate(SEARCH_VALUES):
        for right_key, right_values in SEARCH_VALUES[left_index + 1 :]:
            for left_value in left_values:
                for right_value in right_values:
                    raw.append({left_key: left_value, right_key: right_value})
    raw.extend(dict(parameters) for parameters in CURATED_ENTRY_PROPOSALS)

    proposals: list[Proposal] = []
    seen: set[str] = set()
    for parameters in raw:
        if LOCKED_SESSION_PARAMETERS.intersection(parameters):
            raise RuntimeError("Locked production session parameter reached proposal space")
        signature = parameter_signature(parameters)
        if signature in seen:
            continue
        seen.add(signature)
        detail = ", ".join(f"{key}={value}" for key, value in parameters.items())
        proposals.append(
            Proposal(
                name=f"grid-{len(proposals) + 1:04d}",
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


def _round_robin_families(
    rows: Iterable[tuple[int, int, Proposal]],
) -> list[tuple[int, int, Proposal]]:
    """Interleave parameter families while preserving rank and stable order."""

    by_rank: dict[int, dict[str, deque[tuple[int, int, Proposal]]]] = defaultdict(
        lambda: defaultdict(deque)
    )
    family_first_index: dict[tuple[int, str], int] = {}
    for rank, original_index, proposal in rows:
        family = proposal_family(proposal.parameters)
        by_rank[rank][family].append((rank, original_index, proposal))
        family_first_index.setdefault((rank, family), original_index)

    ordered: list[tuple[int, int, Proposal]] = []
    for rank in sorted(by_rank):
        families = sorted(
            by_rank[rank], key=lambda family: family_first_index[(rank, family)]
        )
        while any(by_rank[rank][family] for family in families):
            for family in families:
                bucket = by_rank[rank][family]
                if bucket:
                    ordered.append(bucket.popleft())
    return ordered


def select_proposals(
    ledger: Path,
    batch_size: int,
    playbook: Path = HERE / "playbook.md",
) -> list[Proposal]:
    """Select a diverse batch while respecting tried signatures and the playbook."""

    tested = tested_signatures(ledger)
    priorities, blocked = playbook_guidance(playbook)
    priority_order = {category: index for index, category in enumerate(priorities)}
    category_rows: dict[str, list[tuple[int, int, Proposal]]] = defaultdict(list)

    for original_index, proposal in enumerate(proposal_space()):
        if parameter_signature(proposal.parameters) in tested:
            continue
        categories = parameter_categories(proposal.parameters)
        if any(category in blocked for category in categories):
            continue
        ranks = [priority_order[category] for category in categories if category in priority_order]
        rank = min(ranks) if ranks else len(priority_order)
        category_rows[idea_category(proposal.parameters)].append(
            (rank, original_index, proposal)
        )

    queues = {
        category: deque(_round_robin_families(rows))
        for category, rows in category_rows.items()
    }
    category_order = sorted(
        queues,
        key=lambda category: (
            min(row[0] for row in category_rows[category]),
            min(row[1] for row in category_rows[category]),
            category,
        ),
    )

    selected: list[Proposal] = []
    while len(selected) < batch_size and any(queues.values()):
        progressed = False
        for category in category_order:
            if len(selected) >= batch_size:
                break
            queue = queues[category]
            if not queue:
                continue
            _, _, proposal = queue.popleft()
            selected.append(proposal)
            progressed = True
        if not progressed:
            break
    return selected


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


def post_discord(message: str, webhook: str | None = None, attempts: int = 3) -> bool:
    """Post with bounded retries; research results remain durable before delivery."""

    target = webhook or os.getenv("DISCORD_WEBHOOK_URL")
    if not target:
        return False
    payload = json.dumps(
        {"content": message, "allowed_mentions": {"parse": []}}
    ).encode("utf-8")
    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(
            target,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "wickless-autoresearch/2",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                if response.status not in {200, 204}:
                    raise RuntimeError(f"Discord returned HTTP {response.status}")
            return True
        except (urllib.error.HTTPError, urllib.error.URLError, RuntimeError):
            if attempt == attempts:
                raise
            time_module.sleep(2 ** (attempt - 1))
    return False


def _metric(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:+.2f}R"


def select_best_experiment(
    outputs: list[dict[str, Any]], policy: dict[str, Any]
) -> dict[str, Any] | None:
    """Return the strongest experiment that changed realized trades."""

    trade_changed = [
        output for output in outputs if output.get("effect") == "trade-changed"
    ]
    if not trade_changed:
        return None
    return max(trade_changed, key=lambda output: objective_tuple(output, policy))


def _name(result: dict[str, Any]) -> str:
    return str(result.get("name") or result.get("candidate") or "unknown")


def _parameter_line(result: dict[str, Any]) -> str:
    parameters = result.get("parameters", {})
    if not parameters:
        return "Parameters: production defaults"
    rendered = ", ".join(f"{key}={value}" for key, value in parameters.items())
    return f"Parameters: `{rendered}`"


def _result_lines(label: str, result: dict[str, Any]) -> tuple[str, str, str, str]:
    folds = result["folds"]
    overall = result["overall"]
    return (
        f"**{label} — {_name(result)}**",
        (
            f"June: {folds['june_2026']['trades']} trades, "
            f"{_metric(folds['june_2026']['net_r'])}"
        ),
        (
            f"July: {folds['july_2026']['trades']} trades, "
            f"{_metric(folds['july_2026']['net_r'])}"
        ),
        (
            f"Overall: {overall['trades']} trades, "
            f"{_metric(overall['net_r'])}, "
            f"drawdown {overall['maximum_drawdown_r']:.2f}R"
        ),
    )


def _delta_line(benchmark: dict[str, Any], best: dict[str, Any]) -> str:
    benchmark_folds = benchmark["folds"]
    best_folds = best["folds"]
    return (
        "Δ vs benchmark: "
        f"June {_metric(best_folds['june_2026']['net_r'] - benchmark_folds['june_2026']['net_r'])} | "
        f"July {_metric(best_folds['july_2026']['net_r'] - benchmark_folds['july_2026']['net_r'])} | "
        f"Overall {_metric(best['overall']['net_r'] - benchmark['overall']['net_r'])} | "
        f"Drawdown {_metric(best['overall']['maximum_drawdown_r'] - benchmark['overall']['maximum_drawdown_r'])}"
    )


def _decision_line(best: dict[str, Any]) -> str:
    status = str(best.get("status", "unknown")).upper()
    if status == "KEEP":
        return "Decision: **KEPT**"
    failed = [
        name.replace("_", " ")
        for name, passed in best.get("acceptance_gates", {}).get("checks", {}).items()
        if not passed
    ]
    if failed:
        return f"Decision: **{status}** — failed: {', '.join(failed)}"
    return f"Decision: **{status}** — did not beat the benchmark objective"


def discord_summary(summary: dict[str, Any]) -> str:
    benchmark = summary.get("benchmark", summary["incumbent"])
    best = summary.get("best_experiment")
    coach_runs = summary.get("coach_runs", [])
    if coach_runs:
        changed = sum(bool(row["changed"]) for row in coach_runs)
        coach_line = (
            f"Coach: ran {len(coach_runs)} time(s), "
            f"updated the playbook {changed} time(s)"
        )
    else:
        coach_line = "Coach: interval not reached"

    coverage = ", ".join(
        f"{name} {count}" for name, count in summary.get("category_counts", {}).items()
    ) or "none"
    lines = [
        f"🔬 **Wickless nightly autoresearch — {summary['london_date']}**",
        (
            f"Tested **{summary['tested']}** | Trade changed **{summary['trade_changed']}** | "
            f"Funnel only **{summary['funnel_only']}** | "
            f"No effect **{summary['no_effect']}** | KEEP **{summary['kept']}**"
        ),
        f"Coverage: {coverage}",
        coach_line,
        *_result_lines("Benchmark", benchmark),
    ]
    if best is None:
        lines.append("**Best trade-changing test — none**")
        if summary.get("funnel_only"):
            lines.append(
                f"{summary['funnel_only']} test(s) changed only the rejection funnel; "
                "realized trades stayed identical."
            )
        elif summary.get("tested"):
            lines.append("Every completed test reproduced benchmark behavior exactly.")
    else:
        lines.extend(
            (
                *_result_lines("Best trade-changing test", best),
                _parameter_line(best),
                _delta_line(benchmark, best),
                _decision_line(best),
            )
        )
    lines.append(f"Results: {REPOSITORY_URL}/tree/{NIGHTLY_BRANCH}")
    message = "\n".join(lines)
    if len(message) > 2000:
        raise RuntimeError("Discord summary exceeds 2,000 characters")
    return message


def keep_message(output: dict[str, Any]) -> str:
    folds = output["folds"]
    overall = output["overall"]
    return "\n".join(
        (
            "✅ **Strong Wickless candidate found**",
            f"Candidate: **{output['candidate']}**",
            _parameter_line(output),
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
        "behavior_sha256": behavior_digest(report),
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
    parser.add_argument("--attempts", type=Path, default=HERE / "attempts.log")
    parser.add_argument("--playbook", type=Path, default=HERE / "playbook.md")
    parser.add_argument("--coach-state", type=Path, default=HERE / "coach_state.json")
    parser.add_argument("--coach-interval", type=int, default=20)
    parser.add_argument("--git-commits", action="store_true")
    parser.add_argument("--no-discord", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not 1 <= args.batch_size <= 20:
        raise ValueError("--batch-size must be between 1 and 20")

    policy = load_policy(args.policy)
    benchmark = _incumbent_summary(args.incumbent) if args.incumbent.exists() else None
    outputs: list[dict[str, Any]] = []
    coach_outputs: list[dict[str, Any]] = []

    def coach_cycle() -> None:
        result = run_coach_if_due(
            attempts_path=args.attempts,
            playbook_path=args.playbook,
            state_path=args.coach_state,
            interval=args.coach_interval,
        )
        if not result.ran:
            return
        coach_outputs.append(result.to_dict())
        if args.git_commits:
            git_commit(
                f"autoresearch: coach after {result.attempt_count} attempts",
                [args.playbook, args.coach_state],
            )

    coach_cycle()
    planned = select_proposals(args.ledger, args.batch_size, args.playbook)
    for proposal in planned:
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
            "--attempts",
            str(args.attempts),
        ]
        if commit:
            command.extend(("--commit", commit))
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            code = run_experiment(command)
        output = json.loads(captured.getvalue())
        outputs.append(output)
        if args.git_commits:
            suffix = (
                output["effect"]
                if output["effect"] != "trade-changed"
                else output["status"]
            )
            git_commit(
                f"autoresearch: record {proposal.name} {suffix}",
                [args.ledger, args.incumbent, args.runs, args.attempts],
            )
        if code == 0 and not args.no_discord:
            post_discord(keep_message(output))
        coach_cycle()

    incumbent = _incumbent_summary(args.incumbent)
    if benchmark is None:
        benchmark = incumbent
    best_experiment = select_best_experiment(outputs, policy)
    incumbent_proposal = Proposal(
        name=incumbent["name"],
        description=incumbent["description"],
        parameters=incumbent["parameters"],
    )
    write_candidate(args.candidate, incumbent_proposal)
    generated = datetime.now(UTC).replace(microsecond=0)
    attempt_rows = read_attempts(args.attempts)
    category_counts: dict[str, int] = {}
    for output in outputs:
        category = str(output["category"])
        category_counts[category] = category_counts.get(category, 0) + 1
    trade_changed = sum(
        output["effect"] == "trade-changed" for output in outputs
    )
    funnel_only = sum(
        output["effect"] == "funnel-only" for output in outputs
    )
    no_effect = sum(output["effect"] == "no-effect" for output in outputs)
    summary = {
        "schema_version": 2,
        "generated_at_utc": generated.isoformat(),
        "generated_at_london": generated.astimezone(LONDON).isoformat(),
        "london_date": generated.astimezone(LONDON).date().isoformat(),
        "tested": len(outputs),
        "trade_changed": trade_changed,
        "funnel_only": funnel_only,
        "no_effect": no_effect,
        "kept": sum(output["status"] == "keep" for output in outputs),
        "rejected": sum(output["status"] == "discard" for output in outputs),
        "category_counts": dict(sorted(category_counts.items())),
        "planned_candidates": [proposal.name for proposal in planned],
        "attempts_total": len(attempt_rows),
        "worker_attempts_total": sum(
            row.category != "baseline" for row in attempt_rows
        ),
        "experiments": outputs,
        "coach_runs": coach_outputs,
        "benchmark": benchmark,
        "best_experiment": best_experiment,
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
