#!/usr/bin/env python3
"""Find and persist the strongest valid benchmark when no incumbent exists."""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from autoresearch.attempts import format_score, sync_attempts_from_ledger
from autoresearch.evaluator import (
    Candidate,
    evaluate,
    load_policy,
    objective_tuple,
    report_digest,
    validate_parameters,
)
from autoresearch.nightly_batch import (
    LOCKED_SESSION_PARAMETERS,
    Proposal,
    parameter_signature,
    proposal_space,
)
from autoresearch.run_experiment import (
    _append_ledger,
    _read_ledger,
    _write_json_atomic,
)
from production_session import PRODUCTION_RELEASE_SHA

UTC = timezone.utc
HERE = Path(__file__).resolve().parent


def _candidate(name: str, description: str, parameters: dict[str, Any]) -> Candidate:
    normalized = validate_parameters(parameters)
    encoded = json.dumps(
        {"name": name, "description": description, "parameters": parameters},
        sort_keys=True,
        separators=(",", ":"),
    )
    return Candidate(
        name=name,
        description=description,
        parameters=normalized,
        source_sha256=hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
    )


def frequency_proposals() -> list[Proposal]:
    """Return fixed-session combinations intended to lift monthly trade frequency."""

    rows: list[Proposal] = []
    values = itertools.product(
        (0.70, 0.74, 0.78),
        (0.40, 0.44),
        (2.20, 3.00),
        (0.12, 0.16, 0.20),
        ("ema_slope", "none"),
        (0.5, 1.0),
        (1.25, 1.75),
    )
    for index, (
        body,
        minimum_range,
        maximum_range,
        close,
        trend,
        tolerance,
        maximum_wick,
    ) in enumerate(values, 1):
        parameters = {
            "minimum_body_ratio": body,
            "minimum_range_atr": minimum_range,
            "maximum_range_atr": maximum_range,
            "close_location_fraction": close,
            "trend_filter": trend,
            "tolerance_ticks": tolerance,
            "maximum_wick_ticks": maximum_wick,
        }
        if LOCKED_SESSION_PARAMETERS.intersection(parameters):
            raise RuntimeError("Locked production session reached bootstrap proposals")
        rows.append(
            Proposal(
                name=f"bootstrap-{index:04d}",
                description=(
                    "Frequency-focused bootstrap candidate using the fixed London "
                    "and New York production session union."
                ),
                parameters=parameters,
            )
        )
    return rows


def bootstrap_proposal_space() -> list[Proposal]:
    """Return deterministic, unique focused proposals followed by the normal grid."""

    proposals: list[Proposal] = []
    seen: set[str] = set()
    for proposal in [*frequency_proposals(), *proposal_space()]:
        signature = parameter_signature(proposal.parameters)
        if signature in seen:
            continue
        seen.add(signature)
        proposals.append(proposal)
    return proposals


def select_best_passing(
    reports: Iterable[dict[str, Any]], policy: dict[str, Any]
) -> dict[str, Any] | None:
    passing = [report for report in reports if report["acceptance_gates"]["passed"]]
    if not passing:
        return None
    return max(passing, key=lambda report: objective_tuple(report, policy))


def _compact(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "candidate": report["candidate"],
        "objective": report["objective"],
        "acceptance_gates": report["acceptance_gates"],
        "overall": report["overall"],
        "folds": {
            name: value["metrics"] for name, value in report["folds"].items()
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument("--policy", type=Path, default=HERE / "policy.json")
    parser.add_argument("--ledger", type=Path, default=HERE / "results.jsonl")
    parser.add_argument("--attempts", type=Path, default=HERE / "attempts.log")
    parser.add_argument("--incumbent", type=Path, default=HERE / "incumbent.json")
    parser.add_argument("--runs", type=Path, default=HERE / "runs")
    parser.add_argument(
        "--results", type=Path, default=HERE / "runs" / "bootstrap-results.json"
    )
    parser.add_argument(
        "--summary", type=Path, default=HERE / "runs" / "bootstrap-summary.json"
    )
    parser.add_argument("--max-candidates", type=int, default=0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.incumbent.exists():
        raise SystemExit("A valid incumbent already exists; bootstrap is not allowed.")

    policy = load_policy(args.policy)
    proposals = bootstrap_proposal_space()
    if args.max_candidates:
        if args.max_candidates < 1:
            raise ValueError("--max-candidates must be positive or zero for all")
        proposals = proposals[: args.max_candidates]

    args.results.parent.mkdir(parents=True, exist_ok=True)
    compact_results: list[dict[str, Any]] = []
    best_report: dict[str, Any] | None = None
    passing_count = 0
    production_defaults = _candidate(
        "production-defaults",
        "Unmodified production defaults checked against bootstrap gates.",
        {},
    )
    candidates = [
        production_defaults,
        *(
            _candidate(proposal.name, proposal.description, proposal.parameters)
            for proposal in proposals
        ),
    ]

    for index, candidate in enumerate(candidates, 1):
        report = evaluate(candidate, data_root=args.data_root, policy=policy)
        compact_results.append(_compact(report))
        if report["acceptance_gates"]["passed"]:
            passing_count += 1
            if best_report is None or objective_tuple(report, policy) > objective_tuple(
                best_report, policy
            ):
                best_report = report
        print(
            json.dumps(
                {
                    "tested": index,
                    "candidate": candidate.name,
                    "passed": report["acceptance_gates"]["passed"],
                    "june_trades": report["folds"]["june_2026"]["metrics"]["trades"],
                    "july_trades": report["folds"]["july_2026"]["metrics"]["trades"],
                    "net_r": report["overall"]["net_r"],
                },
                sort_keys=True,
            ),
            flush=True,
        )

    _write_json_atomic(args.results, {"schema_version": 2, "results": compact_results})
    winner = best_report
    generated_at = datetime.now(UTC).replace(microsecond=0).isoformat()
    if winner is None:
        summary = {
            "schema_version": 2,
            "generated_at_utc": generated_at,
            "production_release_sha": PRODUCTION_RELEASE_SHA,
            "tested": len(compact_results),
            "passing": 0,
            "selected": None,
            "session_policy": "fixed London-or-New-York production union",
        }
        _write_json_atomic(args.summary, summary)
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0

    selected_source = winner["candidate"]
    baseline_candidate = _candidate(
        "production-baseline",
        f"Bootstrap-selected from {selected_source['name']} under the 10-trade monthly gates.",
        selected_source["parameters"],
    )
    baseline_report = evaluate(
        baseline_candidate, data_root=args.data_root, policy=policy
    )
    if not baseline_report["acceptance_gates"]["passed"]:
        raise RuntimeError("Selected bootstrap candidate failed when re-evaluated as baseline")
    if objective_tuple(baseline_report, policy) != objective_tuple(winner, policy):
        raise RuntimeError("Bootstrap baseline re-evaluation changed the objective")

    run_key = f"bootstrap|{generated_at}|{baseline_candidate.source_sha256}"
    run_id = hashlib.sha256(run_key.encode("utf-8")).hexdigest()[:16]
    args.runs.mkdir(parents=True, exist_ok=True)
    _write_json_atomic(args.runs / f"{run_id}.json", baseline_report)
    record = {
        "schema_version": 2,
        "run_id": run_id,
        "generated_at_utc": generated_at,
        "commit": "bootstrap-search",
        "production_release_sha": PRODUCTION_RELEASE_SHA,
        "candidate": baseline_report["candidate"],
        "category": "baseline",
        "score": format_score(baseline_report["objective"]),
        "status": "keep",
        "effect": "baseline",
        "report_sha256": report_digest(baseline_report),
        "objective": baseline_report["objective"],
        "acceptance_gates": baseline_report["acceptance_gates"],
        "incumbent_before": None,
        "incumbent_after": "production-baseline",
        "bootstrap_source": selected_source["name"],
    }
    _append_ledger(args.ledger, record)
    _write_json_atomic(
        args.incumbent,
        {
            "schema_version": 2,
            "updated_at_utc": generated_at,
            "source_run_id": run_id,
            "production_release_sha": PRODUCTION_RELEASE_SHA,
            "bootstrap_source": selected_source["name"],
            "report": baseline_report,
        },
    )
    records, _ = _read_ledger(args.ledger)
    sync_attempts_from_ledger(args.attempts, records)
    summary = {
        "schema_version": 2,
        "generated_at_utc": generated_at,
        "production_release_sha": PRODUCTION_RELEASE_SHA,
        "tested": len(compact_results),
        "passing": passing_count,
        "session_policy": "fixed London-or-New-York production union",
        "selected": {
            "source_candidate": selected_source["name"],
            "parameters": baseline_report["candidate"]["parameters"],
            "objective": baseline_report["objective"],
            "acceptance_gates": baseline_report["acceptance_gates"],
            "overall": baseline_report["overall"],
            "folds": {
                name: value["metrics"]
                for name, value in baseline_report["folds"].items()
            },
        },
    }
    _write_json_atomic(args.summary, summary)
    print(json.dumps(summary, indent=2, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
