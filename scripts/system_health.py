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
    policy_release = policy["production_baseline_sha"]
    candidate = load_candidate(root / "autoresearch" / "candidate.py")
    live_workflow = (root / ".github/workflows/live-signals.yml").read_text(
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
            "single_flight_live_scans",
            "cancel-in-progress: true" in live_workflow,
            "live scan concurrency cancels overlapping work",
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
        checks.append(
            _check(
                "incumbent_release",
                incumbent_release == PRODUCTION_RELEASE_SHA,
                f"incumbent={incumbent_release or 'unstamped'} production={PRODUCTION_RELEASE_SHA}",
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

    failures = [check for check in checks if check.status == "fail"]
    warnings = [check for check in checks if check.status == "warning"]
    return {
        "schema_version": 1,
        "generated_at_utc": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "healthy": not failures,
        "failures": len(failures),
        "warnings": len(warnings),
        "regular_proposals": len(regular),
        "bootstrap_proposals": len(bootstrap),
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
