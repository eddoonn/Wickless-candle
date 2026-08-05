#!/usr/bin/env python3
"""Evaluate one candidate, apply keep/reject policy, and append an audit record."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from autoresearch.attempts import format_score, idea_category, sync_attempts_from_ledger
from autoresearch.behavior import behavior_digest, outcome_digest
from autoresearch.evaluator import (
    candidate_beats,
    evaluate,
    load_candidate,
    load_policy,
    report_digest,
)
from production_session import PRODUCTION_RELEASE_SHA


UTC = timezone.utc
HERE = Path(__file__).resolve().parent


def _git_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=HERE.parent,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "uncommitted"


def _write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _read_ledger(path: Path) -> tuple[list[dict[str, Any]], str]:
    records: list[dict[str, Any]] = []
    previous = "0" * 64
    if not path.exists():
        return records, previous
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        record = json.loads(line)
        claimed = record.pop("record_sha256", None)
        if record.get("previous_sha256") != previous:
            raise ValueError(f"Ledger chain broken at line {line_number}")
        encoded = json.dumps(record, sort_keys=True, separators=(",", ":"))
        actual = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        if claimed != actual:
            raise ValueError(f"Ledger digest mismatch at line {line_number}")
        record["record_sha256"] = claimed
        records.append(record)
        previous = claimed
    return records, previous


def _append_ledger(path: Path, record: dict[str, Any]) -> dict[str, Any]:
    _, previous = _read_ledger(path)
    payload = {"previous_sha256": previous, **record}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    payload["record_sha256"] = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return payload


def _validation_profile(report: dict[str, Any] | None) -> str | None:
    if report is None:
        return None
    return report.get("validation", {}).get("profile_sha256")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument("--candidate", type=Path, default=HERE / "candidate.py")
    parser.add_argument("--policy", type=Path, default=HERE / "policy.json")
    parser.add_argument("--ledger", type=Path, default=HERE / "results.jsonl")
    parser.add_argument("--incumbent", type=Path, default=HERE / "incumbent.json")
    parser.add_argument("--runs", type=Path, default=HERE / "runs")
    parser.add_argument("--attempts", type=Path, default=HERE / "attempts.log")
    parser.add_argument(
        "--commit",
        help="Explicit 40-character source commit for connector-only environments",
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    policy = load_policy(args.policy)
    candidate = load_candidate(args.candidate)
    existing_records: list[dict[str, Any]] = []
    if not args.dry_run:
        existing_records, _ = _read_ledger(args.ledger)
        sync_attempts_from_ledger(args.attempts, existing_records)

    baseline_created = not args.incumbent.exists()
    incumbent_payload = (
        None
        if baseline_created
        else json.loads(args.incumbent.read_text(encoding="utf-8"))
    )
    incumbent_report = None if incumbent_payload is None else incumbent_payload["report"]
    report = evaluate(candidate, data_root=args.data_root, policy=policy)
    validation_profile = _validation_profile(report)
    incumbent_profile = _validation_profile(incumbent_report)
    if validation_profile is not None and incumbent_report is not None:
        if incumbent_profile != validation_profile:
            raise ValueError(
                "Incumbent validation profile is stale; refresh the production reference first"
            )

    report_behavior = behavior_digest(report)
    report_outcome = outcome_digest(report)
    incumbent_behavior = (
        behavior_digest(incumbent_report) if incumbent_report is not None else None
    )
    incumbent_outcome = (
        outcome_digest(incumbent_report) if incumbent_report is not None else None
    )
    if incumbent_behavior is None:
        effect = "baseline"
    elif report_behavior == incumbent_behavior:
        effect = "no-effect"
    elif report_outcome == incumbent_outcome:
        effect = "funnel-only"
    else:
        effect = "trade-changed"

    if baseline_created:
        baseline_qualifies = (
            candidate.parameters == {} and report["acceptance_gates"]["passed"]
        )
        status = "keep" if baseline_qualifies else "discard"
        selected_report = report if baseline_qualifies else None
    else:
        status = (
            "keep"
            if candidate_beats(report, incumbent_report, policy)
            else "discard"
        )
        selected_report = report if status == "keep" else incumbent_report

    generated_at = datetime.now(UTC).replace(microsecond=0).isoformat()
    commit = args.commit or _git_commit()
    if commit != "uncommitted" and (
        len(commit) != 40 or any(character not in "0123456789abcdef" for character in commit)
    ):
        raise ValueError("--commit must be a lowercase 40-character Git SHA")
    run_key = f"{generated_at}|{commit}|{candidate.source_sha256}"
    run_id = hashlib.sha256(run_key.encode("utf-8")).hexdigest()[:16]
    category = idea_category(report["candidate"]["parameters"])
    score = format_score(report["objective"])
    validation = report.get("validation")
    record = {
        "schema_version": 3 if validation else 2,
        "run_id": run_id,
        "generated_at_utc": generated_at,
        "commit": commit,
        "production_release_sha": PRODUCTION_RELEASE_SHA,
        "validation_profile_sha256": validation_profile,
        "candidate": report["candidate"],
        "category": category,
        "score": score,
        "status": status,
        "effect": effect,
        "behavior_sha256": report_behavior,
        "outcome_sha256": report_outcome,
        "report_sha256": report_digest(report),
        "objective": report["objective"],
        "acceptance_gates": report["acceptance_gates"],
        "validation": validation,
        "incumbent_before": (
            incumbent_report["candidate"]["name"] if incumbent_report else None
        ),
        "incumbent_after": (
            selected_report["candidate"]["name"] if selected_report else None
        ),
    }
    output = {
        "run_id": run_id,
        "status": status,
        "effect": effect,
        "production_release_sha": PRODUCTION_RELEASE_SHA,
        "validation_profile_sha256": validation_profile,
        "behavior_sha256": report_behavior,
        "outcome_sha256": report_outcome,
        "candidate": candidate.name,
        "description": report["candidate"]["description"],
        "parameters": report["candidate"]["parameters"],
        "category": category,
        "score": score,
        "objective": report["objective"],
        "acceptance_gates": report["acceptance_gates"],
        "validation": validation,
        "overall": report["overall"],
        "folds": {
            name: value["metrics"] for name, value in report["folds"].items()
        },
    }
    if not args.dry_run:
        args.runs.mkdir(parents=True, exist_ok=True)
        _write_json_atomic(args.runs / f"{run_id}.json", report)
        _append_ledger(args.ledger, record)
        if selected_report is not None:
            selected_profile = _validation_profile(selected_report)
            _write_json_atomic(
                args.incumbent,
                {
                    "schema_version": 3 if selected_profile else 2,
                    "updated_at_utc": generated_at,
                    "source_run_id": run_id if status == "keep" else None,
                    "production_release_sha": PRODUCTION_RELEASE_SHA,
                    "validation_profile_sha256": selected_profile,
                    "report": selected_report,
                },
            )
        records, _ = _read_ledger(args.ledger)
        sync_attempts_from_ledger(args.attempts, records)
    print(json.dumps(output, indent=2, sort_keys=True, allow_nan=False))
    return 0 if status == "keep" else 3


if __name__ == "__main__":
    raise SystemExit(main())
