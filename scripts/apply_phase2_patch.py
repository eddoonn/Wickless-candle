from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{path}: expected one exact match, found {count}")
    path.write_text(text.replace(old, new), encoding="utf-8")


def regex_once(path: Path, pattern: str, replacement: str) -> None:
    text = path.read_text(encoding="utf-8")
    updated, count = re.subn(pattern, replacement, text, count=1, flags=re.S)
    if count != 1:
        raise RuntimeError(f"{path}: regex match count {count}: {pattern[:80]}")
    path.write_text(updated, encoding="utf-8")


def patch_optimizer() -> None:
    path = ROOT / "autoresearch" / "phase2_optimizer.py"
    regex_once(
        path,
        r"def phase2_settings\(policy: dict\[str, Any\]\) -> dict\[str, Any\] \| None:\n.*?\n    return value\n(?=\n\ndef _objective_tuple)",
        '''def phase2_settings(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("Phase 2 optimizer policy must be an object")
    if value.get("schema_version") != 1:
        raise ValueError("Unsupported Phase 2 optimizer policy schema")
    required = {
        "profile",
        "minimum_observations",
        "exploration_fraction",
        "neighbour_count",
        "kernel_bandwidth",
        "uncertainty_weight",
        "diversity_weight",
    }
    missing = sorted(required - set(value))
    if missing:
        raise ValueError("Phase 2 optimizer policy is missing: " + ", ".join(missing))
    exploration = float(value["exploration_fraction"])
    if not 0.20 <= exploration <= 0.50:
        raise ValueError("Phase 2 exploration_fraction must be between 0.20 and 0.50")
    if int(value["minimum_observations"]) < 4:
        raise ValueError("Phase 2 minimum_observations must be at least four")
    if int(value["neighbour_count"]) < 3:
        raise ValueError("Phase 2 neighbour_count must be at least three")
    if float(value["kernel_bandwidth"]) <= 0:
        raise ValueError("Phase 2 kernel_bandwidth must be positive")
    return value
''',
    )
    replace_once(
        path,
        '''def select_with_surrogate(
    candidates: list[CandidatePoint],
    records: list[dict[str, Any]],
    incumbent_objective: dict[str, Any],
    policy: dict[str, Any],
    batch_size: int,
) -> tuple[list[CandidateScore], dict[str, Any]]:
''',
        '''def select_with_surrogate(
    candidates: list[CandidatePoint],
    records: list[dict[str, Any]],
    incumbent_objective: dict[str, Any],
    policy: dict[str, Any],
    batch_size: int,
    optimizer_policy: dict[str, Any] | None = None,
) -> tuple[list[CandidateScore], dict[str, Any]]:
''',
    )
    replace_once(path, "    settings = phase2_settings(policy)\n", "    settings = phase2_settings(optimizer_policy)\n")
    replace_once(
        path,
        '            "reason": "phase2_optimization is not configured",\n',
        '            "reason": "Phase 2 optimizer policy is not configured",\n',
    )


def patch_nightly() -> None:
    path = ROOT / "autoresearch" / "nightly_batch.py"
    replace_once(
        path,
        '''from autoresearch.evaluator import load_candidate, load_policy, objective_tuple
from autoresearch.run_experiment import _read_ledger, main as run_experiment
''',
        '''from autoresearch.evaluator import load_candidate, load_policy, objective_tuple
from autoresearch.phase2_optimizer import CandidatePoint, select_with_surrogate
from autoresearch.run_experiment import (
    _read_ledger,
    _write_json_atomic,
    main as run_experiment,
)
''',
    )
    replace_once(
        path,
        '''@dataclass(frozen=True)
class Proposal:
    name: str
    description: str
    parameters: dict[str, Any]


''',
        '''@dataclass(frozen=True)
class Proposal:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class BatchPlan:
    proposals: list[Proposal]
    optimizer: dict[str, Any]


''',
    )
    insertion = '''

def _eligible_candidate_points(
    ledger: Path,
    playbook: Path,
) -> list[tuple[Proposal, CandidatePoint]]:
    tested = tested_signatures(ledger)
    priorities, blocked = playbook_guidance(playbook)
    priority_order = {category: index for index, category in enumerate(priorities)}
    eligible: list[tuple[Proposal, CandidatePoint]] = []
    for original_index, proposal in enumerate(proposal_space()):
        if parameter_signature(proposal.parameters) in tested:
            continue
        categories = parameter_categories(proposal.parameters)
        if any(category in blocked for category in categories):
            continue
        ranks = [priority_order[category] for category in categories if category in priority_order]
        priority_rank = min(ranks) if ranks else len(priority_order)
        eligible.append(
            (
                proposal,
                CandidatePoint(
                    name=proposal.name,
                    parameters=proposal.parameters,
                    category=idea_category(proposal.parameters),
                    priority_rank=priority_rank,
                    original_index=original_index,
                ),
            )
        )
    return eligible


def plan_proposals(
    ledger: Path,
    batch_size: int,
    playbook: Path,
    *,
    policy: dict[str, Any],
    incumbent_objective: dict[str, Any] | None,
    optimizer_policy: dict[str, Any] | None,
) -> BatchPlan:
    """Use Phase 2 when trained; otherwise preserve diversified selection."""

    fallback = select_proposals(ledger, batch_size, playbook)
    records, _ = _read_ledger(ledger)
    eligible = _eligible_candidate_points(ledger, playbook)
    scored = []
    diagnostics: dict[str, Any]
    if incumbent_objective is None:
        diagnostics = {
            "schema_version": 1,
            "model_version": "phase2-constrained-kernel-v1",
            "mode": "diversified-fallback",
            "observations": 0,
            "candidate_pool": len(eligible),
            "reason": "incumbent objective is unavailable",
            "exploit_count": 0,
            "explore_count": len(fallback),
            "selected": [],
        }
    else:
        scored, diagnostics = select_with_surrogate(
            [point for _, point in eligible],
            records,
            incumbent_objective,
            policy,
            batch_size,
            optimizer_policy,
        )
    if scored:
        proposals_by_name = {proposal.name: proposal for proposal, _ in eligible}
        planned = [proposals_by_name[row.candidate.name] for row in scored]
    else:
        planned = fallback
    diagnostics = dict(diagnostics)
    diagnostics["planned_candidates"] = [
        {
            "name": proposal.name,
            "parameters": proposal.parameters,
            "selection": (
                next(
                    (
                        row.selection
                        for row in scored
                        if row.candidate.name == proposal.name
                    ),
                    "diversified",
                )
            ),
        }
        for proposal in planned
    ]
    return BatchPlan(proposals=planned, optimizer=diagnostics)
'''
    replace_once(path, "\n\ndef render_candidate(proposal: Proposal) -> str:\n", insertion + "\n\ndef render_candidate(proposal: Proposal) -> str:\n")
    replace_once(
        path,
        '''    lines = [
        f"🔬 **Wickless nightly autoresearch — {summary['london_date']}**",
''',
        '''    optimizer = summary.get("optimizer", {})
    optimizer_mode = optimizer.get("mode", "unknown")
    if optimizer_mode == "constrained-surrogate":
        optimizer_line = (
            f"Optimizer: Phase 2 | exploit {optimizer.get('exploit_count', 0)} | "
            f"explore {optimizer.get('explore_count', 0)} | "
            f"observations {optimizer.get('observations', 0)}"
        )
    else:
        optimizer_line = (
            f"Optimizer: diversified fallback | observations "
            f"{optimizer.get('observations', 0)}/"
            f"{optimizer.get('minimum_observations', 'n/a')}"
        )
    lines = [
        f"🔬 **Wickless nightly autoresearch — {summary['london_date']}**",
''',
    )
    replace_once(path, '        f"Coverage: {coverage}",\n        coach_line,\n', '        f"Coverage: {coverage}",\n        optimizer_line,\n        coach_line,\n')
    replace_once(
        path,
        '''        "acceptance_gates": report["acceptance_gates"],
    }
''',
        '''        "acceptance_gates": report["acceptance_gates"],
        "objective": report["objective"],
        "validation": report.get("validation"),
    }
''',
    )
    replace_once(
        path,
        '''    parser.add_argument("--playbook", type=Path, default=HERE / "playbook.md")
    parser.add_argument("--coach-state", type=Path, default=HERE / "coach_state.json")
''',
        '''    parser.add_argument("--playbook", type=Path, default=HERE / "playbook.md")
    parser.add_argument(
        "--optimizer-policy",
        type=Path,
        default=HERE / "optimizer_policy.json",
    )
    parser.add_argument(
        "--optimizer-state",
        type=Path,
        default=HERE / "runs" / "optimizer-state.json",
    )
    parser.add_argument("--coach-state", type=Path, default=HERE / "coach_state.json")
''',
    )
    replace_once(
        path,
        '    planned = select_proposals(args.ledger, args.batch_size, args.playbook)\n',
        '''    optimizer_policy = json.loads(args.optimizer_policy.read_text(encoding="utf-8"))
    plan = plan_proposals(
        args.ledger,
        args.batch_size,
        args.playbook,
        policy=policy,
        incumbent_objective=(benchmark["objective"] if benchmark else None),
        optimizer_policy=optimizer_policy,
    )
    planned = plan.proposals
    optimizer_diagnostics = plan.optimizer
''',
    )
    replace_once(
        path,
        '''    no_effect = sum(output["effect"] == "no-effect" for output in outputs)
    summary = {
''',
        '''    no_effect = sum(output["effect"] == "no-effect" for output in outputs)
    optimizer_state = {
        **optimizer_diagnostics,
        "generated_at_utc": generated.isoformat(),
        "generated_at_london": generated.astimezone(LONDON).isoformat(),
        "outcomes": [
            {
                "candidate": output["candidate"],
                "status": output["status"],
                "effect": output["effect"],
                "acceptance_passed": output["acceptance_gates"]["passed"],
                "objective": output["objective"],
            }
            for output in outputs
        ],
    }
    _write_json_atomic(args.optimizer_state, optimizer_state)
    summary = {
''',
    )
    replace_once(
        path,
        '        "coach_runs": coach_outputs,\n        "benchmark": benchmark,\n',
        '        "coach_runs": coach_outputs,\n        "optimizer": optimizer_state,\n        "benchmark": benchmark,\n',
    )


def patch_health() -> None:
    path = ROOT / "scripts" / "system_health.py"
    replace_once(
        path,
        '''from autoresearch.phase1_validation import policy_profile_sha256
from autoresearch.reference_benchmark import PRODUCTION_BASELINE_SHA
''',
        '''from autoresearch.phase1_validation import policy_profile_sha256
from autoresearch.phase2_optimizer import MODEL_VERSION, phase2_settings
from autoresearch.reference_benchmark import PRODUCTION_BASELINE_SHA
''',
    )
    replace_once(
        path,
        '''    phase1_policy = load_policy(root / "autoresearch" / "walk_forward_policy.json")
    policy_release = policy["production_baseline_sha"]
''',
        '''    phase1_policy = load_policy(root / "autoresearch" / "walk_forward_policy.json")
    optimizer_policy = json.loads(
        (root / "autoresearch" / "optimizer_policy.json").read_text(encoding="utf-8")
    )
    optimizer_settings = phase2_settings(optimizer_policy)
    optimizer_source = (root / "autoresearch" / "phase2_optimizer.py").read_text(
        encoding="utf-8"
    )
    nightly_source = (root / "autoresearch" / "nightly_batch.py").read_text(
        encoding="utf-8"
    )
    policy_release = policy["production_baseline_sha"]
''',
    )
    replace_once(
        path,
        '''        _check(
            "phase1_workflow_dataset",
''',
        '''        _check(
            "phase2_constrained_optimizer",
            optimizer_settings is not None
            and optimizer_settings["profile"] == "phase2-constrained-surrogate-v1"
            and float(optimizer_settings["exploration_fraction"]) >= 0.20
            and int(optimizer_settings["minimum_observations"]) >= 4
            and "validation_profile_sha256" in optimizer_source
            and "select_with_surrogate" in nightly_source
            and "optimizer-state.json" in nightly_source
            and "candidate_beats" not in optimizer_source,
            (
                f"model={MODEL_VERSION} exploration="
                f"{optimizer_settings['exploration_fraction'] if optimizer_settings else 'invalid'}"
            ),
        ),
        _check(
            "phase1_workflow_dataset",
''',
    )
    replace_once(
        path,
        '        "phase1_validation_profile_sha256": phase1_profile,\n        "checks": [asdict(check) for check in checks],\n',
        '        "phase1_validation_profile_sha256": phase1_profile,\n        "phase2_model_version": MODEL_VERSION,\n        "phase2_optimizer_profile": (optimizer_settings or {}).get("profile"),\n        "checks": [asdict(check) for check in checks],\n',
    )


def patch_program() -> None:
    path = ROOT / "autoresearch" / "program.md"
    replace_once(
        path,
        '''Temporarily edit only the literal `candidate.py`, evaluate it with the Phase 1 policy over 12 chronological monthly BID/ASK folds, append the report and ledgers, and keep only candidates that pass every legacy and Phase 1 gate and beat the incumbent. The June and July 10-trade gates remain mandatory. After the batch, restore the neutral production candidate. The workflow verifies the changed-file allowlist before committing one audit update to `main`.
''',
        '''Temporarily edit only the literal `candidate.py`, evaluate it with the Phase 1 policy over 12 chronological monthly BID/ASK folds, append the report and ledgers, and keep only candidates that pass every legacy and Phase 1 gate and beat the incumbent. The June and July 10-trade gates remain mandatory. Phase 2 ranks the bounded untested proposal pool with an exact-profile constrained surrogate, reserves at least 20% of each trained batch for uncertainty-led exploration, and falls back to deterministic diversification until enough comparable observations exist. The optimiser may choose tests but can never promote a candidate or change a gate. After the batch, restore the neutral production candidate. The workflow verifies the changed-file allowlist before committing one audit update to `main`.
''',
    )


def write_tests() -> None:
    path = ROOT / "tests" / "test_phase2_optimizer.py"
    path.write_text(
        '''from __future__ import annotations

import json
import unittest
from pathlib import Path

from autoresearch.phase1_validation import policy_profile_sha256
from autoresearch.phase2_optimizer import (
    CandidatePoint,
    comparable_observations,
    parameter_distance,
    phase2_settings,
    select_with_surrogate,
)


ROOT = Path(__file__).resolve().parents[1]


def objective(value: float, trades: int = 80) -> dict:
    return {
        "worst_fold_net_r": value,
        "total_net_r": value * 12,
        "overall_profit_factor": 1.5 + max(0.0, value) / 10,
        "negative_overall_drawdown_r": -3.0,
        "total_trades": trades,
    }


def record(policy: dict, index: int, body: float, passed: bool, value: float) -> dict:
    checks = {
        "june_2026_minimum_trades": passed,
        "july_2026_minimum_trades": passed,
        "minimum_total_trades": passed,
        "minimum_overall_profit_factor": passed,
        "maximum_overall_drawdown_r": passed,
        "minimum_profitable_fold_ratio": passed,
        "neighbourhood_robustness": passed,
    }
    return {
        "run_id": f"run-{index}",
        "production_release_sha": policy["production_baseline_sha"],
        "validation_profile_sha256": policy_profile_sha256(policy),
        "candidate": {"parameters": {"minimum_body_ratio": body}},
        "objective": objective(value),
        "acceptance_gates": {"passed": passed, "checks": checks},
        "effect": "trade-changed",
    }


class Phase2OptimizerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = json.loads(
            (ROOT / "autoresearch" / "walk_forward_policy.json").read_text(
                encoding="utf-8"
            )
        )
        self.optimizer = json.loads(
            (ROOT / "autoresearch" / "optimizer_policy.json").read_text(
                encoding="utf-8"
            )
        )

    def test_policy_requires_twenty_percent_exploration(self) -> None:
        settings = phase2_settings(self.optimizer)
        self.assertGreaterEqual(settings["exploration_fraction"], 0.20)
        invalid = dict(self.optimizer)
        invalid["exploration_fraction"] = 0.10
        with self.assertRaises(ValueError):
            phase2_settings(invalid)

    def test_only_exact_validation_profile_records_train_the_model(self) -> None:
        current = record(self.policy, 1, 0.78, True, 1.0)
        stale = dict(current)
        stale["run_id"] = "stale"
        stale["validation_profile_sha256"] = "0" * 64
        observations = comparable_observations([current, stale], self.policy)
        self.assertEqual([row.run_id for row in observations], ["run-1"])

    def test_parameter_distance_uses_defaults_for_missing_values(self) -> None:
        keys = ("minimum_body_ratio", "trend_filter")
        self.assertEqual(parameter_distance({}, {}, keys), 0.0)
        self.assertGreater(
            parameter_distance(
                {"minimum_body_ratio": 0.75},
                {"minimum_body_ratio": 0.90},
                keys,
            ),
            0.0,
        )

    def test_falls_back_until_enough_phase1_observations_exist(self) -> None:
        candidates = [
            CandidatePoint("a", {"minimum_body_ratio": 0.78}, "candle-quality", 0, 0)
        ]
        selected, diagnostics = select_with_surrogate(
            candidates,
            [record(self.policy, 1, 0.78, True, 1.0)],
            objective(0.5),
            self.policy,
            1,
            self.optimizer,
        )
        self.assertEqual(selected, [])
        self.assertEqual(diagnostics["mode"], "diversified-fallback")

    def test_trained_batch_is_deterministic_and_reserves_exploration(self) -> None:
        records = [
            record(self.policy, index, 0.76 + index * 0.005, index < 5, 1.0 - index * 0.1)
            for index in range(8)
        ]
        candidates = [
            CandidatePoint(
                f"candidate-{index}",
                {"minimum_body_ratio": 0.75 + index * 0.01},
                "candle-quality",
                0,
                index,
            )
            for index in range(10)
        ]
        first, first_diagnostics = select_with_surrogate(
            candidates, records, objective(0.5), self.policy, 5, self.optimizer
        )
        second, second_diagnostics = select_with_surrogate(
            candidates, records, objective(0.5), self.policy, 5, self.optimizer
        )
        self.assertEqual(
            [row.candidate.name for row in first],
            [row.candidate.name for row in second],
        )
        self.assertEqual(first_diagnostics["mode"], "constrained-surrogate")
        self.assertEqual(first_diagnostics["explore_count"], 1)
        self.assertEqual(first_diagnostics, second_diagnostics)

    def test_exploitation_prefers_the_locally_feasible_region(self) -> None:
        records = [
            *[
                record(self.policy, index, 0.76 + index * 0.005, True, 1.0 + index * 0.1)
                for index in range(4)
            ],
            *[
                record(self.policy, index + 4, 0.88 + index * 0.005, False, -2.0 - index)
                for index in range(4)
            ],
        ]
        candidates = [
            CandidatePoint("near-feasible", {"minimum_body_ratio": 0.79}, "candle-quality", 0, 0),
            CandidatePoint("near-failing", {"minimum_body_ratio": 0.91}, "candle-quality", 0, 1),
            CandidatePoint("middle", {"minimum_body_ratio": 0.84}, "candle-quality", 0, 2),
        ]
        selected, diagnostics = select_with_surrogate(
            candidates, records, objective(0.5), self.policy, 2, self.optimizer
        )
        exploited = [row for row in selected if row.selection == "exploit"]
        self.assertEqual(diagnostics["exploit_count"], 1)
        self.assertEqual(exploited[0].candidate.name, "near-feasible")
        self.assertGreater(
            exploited[0].feasibility_probability,
            next(row for row in selected if row.selection == "explore").feasibility_probability,
        )

    def test_nightly_persists_optimizer_state_under_allowlisted_runs(self) -> None:
        source = (ROOT / "autoresearch" / "nightly_batch.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("select_with_surrogate", source)
        self.assertIn('HERE / "runs" / "optimizer-state.json"', source)
        self.assertIn('"optimizer": optimizer_state', source)


if __name__ == "__main__":
    unittest.main()
''',
        encoding="utf-8",
    )


def main() -> None:
    patch_optimizer()
    patch_nightly()
    patch_health()
    patch_program()
    write_tests()


if __name__ == "__main__":
    main()
