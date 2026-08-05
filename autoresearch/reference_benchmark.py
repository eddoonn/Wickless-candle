#!/usr/bin/env python3
"""Create or preserve a production reference benchmark for nightly research."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from autoresearch.attempts import format_score, sync_attempts_from_ledger
from autoresearch.evaluator import Candidate, evaluate, load_policy, report_digest
from autoresearch.run_experiment import (
    _append_ledger,
    _git_commit,
    _read_ledger,
    _write_json_atomic,
)
from production_session import PRODUCTION_RELEASE_SHA

UTC = timezone.utc
HERE = Path(__file__).resolve().parent
PRODUCTION_BASELINE_SHA = PRODUCTION_RELEASE_SHA


def production_reference_candidate() -> Candidate:
    identity = f"production-baseline:{PRODUCTION_BASELINE_SHA}"
    return Candidate(
        name="production-baseline",
        description=(
            "Immutable Wickless production defaults with the London-New York "
            f"session union at {PRODUCTION_BASELINE_SHA[:8]}."
        ),
        parameters={},
        source_sha256=hashlib.sha256(identity.encode("utf-8")).hexdigest(),
    )


def _load_usable_incumbent(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        report = payload["report"]
        candidate = report["candidate"]
        if not isinstance(candidate["name"], str) or not candidate["name"]:
            return None
        if not isinstance(candidate["parameters"], dict):
            return None
        for field in ("folds", "overall", "objective", "acceptance_gates"):
            if field not in report:
                return None
        return payload
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def ensure_reference_benchmark(
    *,
    data_root: Path,
    policy_path: Path,
    ledger_path: Path,
    incumbent_path: Path,
    runs_path: Path,
    attempts_path: Path,
    force: bool = False,
) -> tuple[dict[str, Any], bool]:
    """Return an incumbent, creating production defaults as a reference when needed.

    Acceptance gates continue to apply to every experiment. The reference benchmark
    is allowed to miss a newer candidate gate because it is the comparison point,
    not a candidate being promoted.
    """

    existing = _load_usable_incumbent(incumbent_path)
    if existing is not None and not force:
        return existing, False

    policy = load_policy(policy_path)
    candidate = production_reference_candidate()
    report = evaluate(candidate, data_root=data_root, policy=policy)
    generated_at = datetime.now(UTC).replace(microsecond=0).isoformat()
    commit = _git_commit()
    run_key = f"reference|{generated_at}|{commit}|{candidate.source_sha256}"
    run_id = hashlib.sha256(run_key.encode("utf-8")).hexdigest()[:16]

    runs_path.mkdir(parents=True, exist_ok=True)
    _write_json_atomic(runs_path / f"{run_id}.json", report)
    record = {
        "schema_version": 1,
        "run_id": run_id,
        "generated_at_utc": generated_at,
        "commit": commit,
        "candidate": report["candidate"],
        "category": "baseline",
        "score": format_score(report["objective"]),
        "status": "keep",
        "benchmark_role": "production-reference",
        "report_sha256": report_digest(report),
        "objective": report["objective"],
        "acceptance_gates": report["acceptance_gates"],
        "incumbent_before": (
            existing["report"]["candidate"]["name"] if existing else None
        ),
        "incumbent_after": report["candidate"]["name"],
    }
    _append_ledger(ledger_path, record)
    payload = {
        "schema_version": 1,
        "updated_at_utc": generated_at,
        "source_run_id": run_id,
        "benchmark_role": "production-reference",
        "report": report,
    }
    _write_json_atomic(incumbent_path, payload)
    records, _ = _read_ledger(ledger_path)
    sync_attempts_from_ledger(attempts_path, records)
    return payload, True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument("--policy", type=Path, default=HERE / "policy.json")
    parser.add_argument("--ledger", type=Path, default=HERE / "results.jsonl")
    parser.add_argument("--incumbent", type=Path, default=HERE / "incumbent.json")
    parser.add_argument("--runs", type=Path, default=HERE / "runs")
    parser.add_argument("--attempts", type=Path, default=HERE / "attempts.log")
    parser.add_argument("--force", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload, created = ensure_reference_benchmark(
        data_root=args.data_root,
        policy_path=args.policy,
        ledger_path=args.ledger,
        incumbent_path=args.incumbent,
        runs_path=args.runs,
        attempts_path=args.attempts,
        force=args.force,
    )
    report = payload["report"]
    output = {
        "created": created,
        "benchmark_role": payload.get("benchmark_role", "promoted-incumbent"),
        "candidate": report["candidate"]["name"],
        "acceptance_gates": report["acceptance_gates"],
        "overall": report["overall"],
        "folds": {
            name: value["metrics"] for name, value in report["folds"].items()
        },
    }
    print(json.dumps(output, indent=2, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
