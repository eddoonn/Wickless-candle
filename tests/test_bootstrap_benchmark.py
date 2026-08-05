from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autoresearch.bootstrap_benchmark import (
    bootstrap_proposal_space,
    main,
    select_best_passing,
)
from autoresearch.nightly_batch import (
    LOCKED_SESSION_PARAMETERS,
    Proposal,
    parameter_signature,
)
from production_session import PRODUCTION_RELEASE_SHA


ROOT = Path(__file__).resolve().parents[1]


def report(name: str, parameters: dict, *, passed: bool, score: float) -> dict:
    metrics = {
        "trades": 10,
        "wins": 6,
        "losses": 4,
        "win_rate": 0.6,
        "net_r": score,
        "expectancy_r": score / 10,
        "profit_factor": 2.0,
        "maximum_drawdown_r": 2.0,
        "distinct_pairs": 4,
        "maximum_pair_trade_share": 0.4,
        "trades_by_pair": {
            "EURUSD": 4,
            "GBPUSD": 3,
            "USDJPY": 2,
            "AUDUSD": 1,
        },
    }
    overall = {**metrics, "trades": 20, "net_r": score * 2, "ambiguous_exits": 0}
    return {
        "schema_version": 1,
        "production_baseline_sha": PRODUCTION_RELEASE_SHA,
        "candidate": {
            "name": name,
            "description": "test",
            "parameters": parameters,
            "source_sha256": name,
        },
        "safety": {},
        "folds": {
            "june_2026": {
                "metrics": metrics,
                "counters": {},
                "data_qa": [],
                "window": {},
                "minimum_trades": 10,
            },
            "july_2026": {
                "metrics": metrics,
                "counters": {},
                "data_qa": [],
                "window": {},
                "minimum_trades": 10,
            },
        },
        "overall": overall,
        "objective": {
            "worst_fold_net_r": score,
            "total_net_r": score * 2,
            "overall_profit_factor": 2.0,
            "negative_overall_drawdown_r": -2.0,
            "total_trades": 20,
        },
        "acceptance_gates": {"passed": passed, "checks": {"all": passed}},
        "trades": [],
    }


class BootstrapTests(unittest.TestCase):
    def test_space_is_unique_broad_and_never_mutates_session_clocks(self) -> None:
        proposals = bootstrap_proposal_space()
        signatures = [parameter_signature(row.parameters) for row in proposals]
        self.assertEqual(len(signatures), len(set(signatures)))
        self.assertTrue(proposals[0].name.startswith("bootstrap-"))
        self.assertGreater(len(proposals), 900)
        self.assertTrue(
            all(not LOCKED_SESSION_PARAMETERS.intersection(row.parameters) for row in proposals)
        )

    def test_workflow_supports_full_bootstrap_and_neutral_reset(self) -> None:
        workflow = (
            ROOT / ".github" / "workflows" / "autoresearch-nightly.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("autoresearch/bootstrap-benchmark.request", workflow)
        self.assertIn("python -m autoresearch.bootstrap_benchmark", workflow)
        self.assertIn('--max-candidates "$BOOTSTRAP_LIMIT"', workflow)
        self.assertIn("Restore neutral candidate surface", workflow)
        self.assertIn("timeout-minutes: 360", workflow)

    def test_best_selection_ignores_failed_gates(self) -> None:
        policy = json.loads((ROOT / "autoresearch" / "policy.json").read_text())
        failed = report("failed", {}, passed=False, score=99)
        lower = report("lower", {}, passed=True, score=1)
        higher = report("higher", {}, passed=True, score=2)
        self.assertEqual(
            select_best_passing([failed, lower, higher], policy)["candidate"]["name"],
            "higher",
        )

    def test_main_persists_the_strongest_valid_baseline(self) -> None:
        proposals = [
            Proposal("candidate-a", "a", {"minimum_body_ratio": 0.74}),
            Proposal("candidate-b", "b", {"minimum_body_ratio": 0.70}),
        ]

        def fake_evaluate(candidate, *, data_root, policy):
            if candidate.name == "production-defaults":
                return report(candidate.name, {}, passed=False, score=50)
            if candidate.name == "candidate-a":
                return report(
                    candidate.name,
                    {"minimum_body_ratio": 0.74},
                    passed=True,
                    score=1,
                )
            return report(
                candidate.name,
                {"minimum_body_ratio": 0.70},
                passed=True,
                score=2,
            )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = root / "policy.json"
            policy.write_text(
                (ROOT / "autoresearch" / "policy.json").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            args = [
                "--data-root",
                str(root / "data"),
                "--policy",
                str(policy),
                "--ledger",
                str(root / "results.jsonl"),
                "--attempts",
                str(root / "attempts.log"),
                "--incumbent",
                str(root / "incumbent.json"),
                "--runs",
                str(root / "runs"),
                "--results",
                str(root / "bootstrap-results.json"),
                "--summary",
                str(root / "bootstrap-summary.json"),
            ]
            with patch(
                "autoresearch.bootstrap_benchmark.bootstrap_proposal_space",
                return_value=proposals,
            ), patch(
                "autoresearch.bootstrap_benchmark.evaluate",
                side_effect=fake_evaluate,
            ):
                code = main(args)
            incumbent = json.loads((root / "incumbent.json").read_text())
            summary = json.loads((root / "bootstrap-summary.json").read_text())
            ledger = (root / "results.jsonl").read_text()

        self.assertEqual(code, 0)
        self.assertEqual(incumbent["schema_version"], 2)
        self.assertEqual(incumbent["production_release_sha"], PRODUCTION_RELEASE_SHA)
        self.assertEqual(
            incumbent["report"]["candidate"]["name"], "production-baseline"
        )
        self.assertEqual(
            incumbent["report"]["candidate"]["parameters"],
            {"minimum_body_ratio": 0.70},
        )
        self.assertEqual(summary["selected"]["source_candidate"], "candidate-b")
        self.assertEqual(summary["production_release_sha"], PRODUCTION_RELEASE_SHA)
        self.assertEqual(summary["schema_version"], 2)
        self.assertIn('"category":"baseline"', ledger)


if __name__ == "__main__":
    unittest.main()
