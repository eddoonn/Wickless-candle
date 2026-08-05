from __future__ import annotations

import json
import unittest
from pathlib import Path

from autoresearch import evaluator
from autoresearch.phase1_validation import (
    multiple_testing_diagnostic,
    neighbour_parameter_sets,
    policy_profile_sha256,
    validation_diagnostics,
)


ROOT = Path(__file__).resolve().parents[1]


class Phase1PolicyTests(unittest.TestCase):
    def test_walk_forward_policy_has_twelve_chronological_folds(self) -> None:
        policy = evaluator.load_policy(ROOT / "autoresearch/walk_forward_policy.json")
        folds = policy["folds"]
        self.assertEqual(len(folds), 12)
        self.assertEqual(len({row["directory"] for row in folds}), 1)
        starts = [row["start_utc"] for row in folds]
        self.assertEqual(starts, sorted(starts))
        by_name = {row["name"]: row for row in folds}
        self.assertEqual(by_name["june_2026"]["minimum_trades"], 10)
        self.assertEqual(by_name["july_2026"]["minimum_trades"], 10)
        self.assertEqual(policy["acceptance"]["minimum_total_trades"], 60)
        self.assertEqual(policy["phase1_validation"]["purge_days"], 2)
        self.assertEqual(policy["phase1_validation"]["embargo_days"], 1)

    def test_production_policy_remains_separate_and_legacy(self) -> None:
        production = evaluator.load_policy(ROOT / "autoresearch/policy.json")
        self.assertNotIn("phase1_validation", production)
        self.assertEqual(len(production["folds"]), 2)

    def test_evaluator_decorator_is_installed(self) -> None:
        self.assertTrue(evaluator._PHASE1_VALIDATION_INSTALLED)
        policy = evaluator.load_policy(ROOT / "autoresearch/walk_forward_policy.json")
        self.assertEqual(len(policy_profile_sha256(policy)), 64)


class Phase1DiagnosticTests(unittest.TestCase):
    def setUp(self) -> None:
        self.phase1 = {
            "bootstrap_samples": 500,
            "bootstrap_block_size": 3,
            "confidence_level": 0.9,
        }

    def test_diagnostics_are_deterministic_and_measure_concentration(self) -> None:
        values = [1.0, 0.5, -0.5, 1.5, 0.2, -0.2, 0.7, 0.3, -0.1, 0.6, 0.4, 0.8]
        first = validation_diagnostics(values, phase1=self.phase1)
        second = validation_diagnostics(values, phase1=self.phase1)
        self.assertEqual(first, second)
        self.assertEqual(first["fold_count"], 12)
        self.assertGreater(first["profitable_fold_ratio"], 0.58)
        self.assertGreater(first["bootstrap"]["probability_mean_positive"], 0.8)
        self.assertGreater(first["largest_profitable_fold_share"], 0.0)
        self.assertLessEqual(first["largest_profitable_fold_share"], 1.0)

    def test_multiple_testing_diagnostic_becomes_more_conservative(self) -> None:
        few = multiple_testing_diagnostic(0.95, prior_trials=1)
        many = multiple_testing_diagnostic(0.95, prior_trials=100)
        self.assertGreater(
            few["bonferroni_adjusted_confidence"],
            many["bonferroni_adjusted_confidence"],
        )
        self.assertFalse(many["promotion_gate"])

    def test_neighbourhood_is_bounded_and_cannot_change_sessions(self) -> None:
        candidate = evaluator.Candidate(
            name="phase1-test",
            description="Phase 1 neighbourhood test.",
            parameters=evaluator.validate_parameters(
                {"minimum_body_ratio": 0.82, "trend_filter": "none"}
            ),
            source_sha256="0" * 64,
        )
        variants = neighbour_parameter_sets(evaluator, candidate, maximum=4)
        self.assertGreaterEqual(len(variants), 2)
        self.assertLessEqual(len(variants), 4)
        for parameters in variants:
            self.assertFalse({"use_session", "session_start", "session_end"} & set(parameters))
            evaluator.validate_parameters(parameters)


class Phase1WorkflowContractTests(unittest.TestCase):
    def test_nightly_workflow_uses_walk_forward_policy_and_dataset(self) -> None:
        workflow = (ROOT / ".github/workflows/autoresearch-nightly.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("autoresearch/walk_forward_policy.json", workflow)
        self.assertIn("dukascopy_m1_bidask_2025-05_2026-07", workflow)
        self.assertIn("--start 2025-05-01 --end 2026-07-31", workflow)
        self.assertIn("wickless-autoresearch-data-v2-walk-forward", workflow)


if __name__ == "__main__":
    unittest.main()
