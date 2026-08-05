#!/usr/bin/env python3
"""Audit production, research, report, and workflow invariants."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from autoresearch.bootstrap_benchmark import bootstrap_proposal_space
from autoresearch.evaluator import load_candidate, load_policy
from autoresearch.nightly_batch import LOCKED_SESSION_PARAMETERS, proposal_space
from autoresearch.phase1_validation import policy_profile_sha256
from autoresearch.reference_benchmark import PRODUCTION_BASELINE_SHA
from production_session import PRODUCTION_RELEASE_SHA, SESSION_LABEL


UTC = timezone.utc


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str


def _check(name: str, passed: bool, detail: str, *, warning: bool = False) -> Check:
    return Check(name, "pass" if passed else "warning" if warning else "fail", detail)


def audit(root: Path = ROOT) -> dict[str, Any]:
    regular = proposal_space()
    bootstrap = bootstrap_proposal_space()
    all_proposals = [*regular, *bootstrap]
    locked_hits = [
        row.name
        for row in all_proposals
        if LOCKED_SESSION_PARAMETERS.intersection(row.parameters)
    ]
    policy = load_policy(root / "autoresearch" / "policy.json")
    phase1_policy = load_policy(root / "autoresearch" / "walk_forward_policy.json")
    policy_release = policy["production_baseline_sha"]
    phase1_release = phase1_policy["production_baseline_sha"]
    phase1_profile = policy_profile_sha256(phase1_policy)
    phase1_folds = phase1_policy["folds"]
    phase1_by_name = {row["name"]: row for row in phase1_folds}
    candidate = load_candidate(root / "autoresearch" / "candidate.py")
    live_workflow = (root / ".github/workflows/live-signals.yml").read_text(
        encoding="utf-8"
    )
    scanner_health_workflow = (
        root / ".github/workflows/scanner-health.yml"
    ).read_text(encoding="utf-8")
    scanner_health_script = (root / "scripts/scanner_health.py").read_text(
        encoding="utf-8"
    )
    nightly_workflow = (
        root / ".github/workflows/autoresearch-nightly.yml"
    ).read_text(encoding="utf-8")

    checks = [
        _check(
            "production_release_identity",
            PRODUCTION_BASELINE_SHA == PRODUCTION_RELEASE_SHA,
            f"reference={PRODUCTION_BASELINE_SHA} production={PRODUCTION_RELEASE_SHA}",
        ),
        _check(
            "policy_release_identity",
            policy_release == PRODUCTION_RELEASE_SHA,
            f"policy={policy_release} production={PRODUCTION_RELEASE_SHA}",
        ),
        _check(
            "phase1_policy_release_identity",
            phase1_release == PRODUCTION_RELEASE_SHA,
            f"phase1={phase1_release} production={PRODUCTION_RELEASE_SHA}",
        ),
        _check(
            "production_session_label",
            "Europe/London" in SESSION_LABEL and "America/New_York" in SESSION_LABEL,
            SESSION_LABEL,
        ),
        _check(
            "locked_sessions_absent_from_search",
            not locked_hits,
            "none" if not locked_hits else ", ".join(locked_hits[:10]),
        ),
        _check(
            "neutral_candidate_surface",
            candidate.name == "production-baseline" and candidate.parameters == {},
            f"name={candidate.name} parameters={candidate.parameters}",
        ),
        _check(
            "nightly_never_pauses",
            "Nightly research is paused" not in nightly_workflow
            and "Run worker and coach experiment loop" in nightly_workflow,
            "nightly worker remains active",
        ),
        _check(
            "single_main_branch_automation",
            "FRAMEWORK_BRANCH:" not in nightly_workflow
            and "NIGHTLY_BRANCH:" not in nightly_workflow
            and "ref: autoresearch/framework-v1" not in nightly_workflow
            and 'git push origin "$NIGHTLY_BRANCH"' not in nightly_workflow
            and 'git push origin HEAD:"$MAIN_BRANCH"' in nightly_workflow,
            "autoresearch code and durable audit state use main only",
        ),
        _check(
            "phase1_walk_forward_validation",
            len(phase1_folds) == 12
            and [row["start_utc"] for row in phase1_folds]
            == sorted(row["start_utc"] for row in phase1_folds)
            and len({row["directory"] for row in phase1_folds}) == 1
            and phase1_by_name["june_2026"]["minimum_trades"] == 10
            and phase1_by_name["july_2026"]["minimum_trades"] == 10
            and phase1_policy["phase1_validation"]["purge_days"] >= 1
            and phase1_policy["phase1_validation"]["embargo_days"] >= 1
            and phase1_policy["acceptance"]["minimum_profitable_fold_ratio"] >= 0.58
            and phase1_policy["acceptance"]["minimum_neighbourhood_pass_rate"] >= 0.5,
            (
                f"folds={len(phase1_folds)} profile="
                f"{phase1_policy['phase1_validation']['profile']}"
            ),
        ),
        _check(
            "phase1_workflow_dataset",
            "autoresearch/walk_forward_policy.json" in nightly_workflow
            and "dukascopy_m1_bidask_2025-05_2026-07" in nightly_workflow
            and "--start 2025-05-01 --end 2026-07-31" in nightly_workflow
            and "wickless-autoresearch-data-v2-walk-forward" in nightly_workflow,
            "nightly uses one cached 15-month BID/ASK source for 12 test folds",
        ),
        _check(
            "single_flight_live_scans",
            "cancel-in-progress: true" in live_workflow,
            "live scan concurrency cancels overlapping work",
        ),
        _check(
            "scanner_health_self_recovery",
            "actions: write" in scanner_health_workflow
            and "contents: read" in scanner_health_workflow
            and "contents: write" not in scanner_health_workflow
            and "python scripts/scanner_health.py" in scanner_health_workflow
            and "dispatch_workflow(" in scanner_health_script
            and '"HEALTHY"' in scanner_health_script
            and '"DEGRADED"' in scanner_health_script
            and '"UNHEALTHY"' in scanner_health_script
            and "checkpoint_covered" in scanner_health_script,
            "heartbeat is checkpoint-aware and may dispatch one recovery scan without content writes",
        ),
    ]

    summary_path = root / "reports/production-backtest/latest/summary.json"
    if summary_path.exists():
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        report_release = summary.get("production_release_sha")
        checks.append(
            _check(
                "production_report_release",
                report_release == PRODUCTION_RELEASE_SHA,
                f"report={report_release or 'unstamped'} production={PRODUCTION_RELEASE_SHA}",
                warning=True,
            )
        )
    else:
        checks.append(
            _check(
                "production_report_release",
                False,
                "latest production report is absent",
                warning=True,
            )
        )

    incumbent_path = root / "autoresearch/incumbent.json"
    if incumbent_path.exists():
        incumbent = json.loads(incumbent_path.read_text(encoding="utf-8"))
        incumbent_release = incumbent.get("production_release_sha")
        incumbent_profile = (
            incumbent.get("validation_profile_sha256")
            or incumbent.get("report", {}).get("validation", {}).get("profile_sha256")
        )
        checks.append(
            _check(
                "incumbent_release",
                incumbent_release == PRODUCTION_RELEASE_SHA,
                f"incumbent={incumbent_release or 'unstamped'} production={PRODUCTION_RELEASE_SHA}",
                warning=True,
            )
        )
        checks.append(
            _check(
                "incumbent_phase1_profile",
                incumbent_profile == phase1_profile,
                (
                    f"incumbent={incumbent_profile or 'legacy'} "
                    f"required={phase1_profile}"
                ),
                warning=True,
            )
        )
    else:
        checks.append(
            _check(
                "incumbent_release",
                False,
                "incumbent is created automatically on main by the nightly workflow",
                warning=True,
            )
        )
        checks.append(
            _check(
                "incumbent_phase1_profile",
                False,
                "Phase 1 reference is created automatically by the nightly workflow",
                warning=True,
            )
        )

    failures = [check for check in checks if check.status == "fail"]
    warnings = [check for check in checks if check.status == "warning"]
    return {
        "schema_version": 2,
        "generated_at_utc": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "healthy": not failures,
        "failures": len(failures),
        "warnings": len(warnings),
        "regular_proposals": len(regular),
        "bootstrap_proposals": len(bootstrap),
        "phase1_validation_profile_sha256": phase1_profile,
        "checks": [asdict(check) for check in checks],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = audit()
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report["healthy"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
