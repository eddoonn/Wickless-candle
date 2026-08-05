from __future__ import annotations

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
