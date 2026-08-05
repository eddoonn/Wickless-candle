from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class MachineLearningWorkflowContractTests(unittest.TestCase):
    def test_system_health_watches_and_compiles_machine_learning(self) -> None:
        workflow = (ROOT / ".github/workflows/system-health.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("- wickless_bot.py", workflow)
        self.assertIn("- wickless_ml/**", workflow)
        self.assertIn("- .github/workflows/ml-learning.yml", workflow)
        self.assertIn("- .github/workflows/ml-live-monitor.yml", workflow)
        self.assertIn("autoresearch wickless_ml scripts tests", workflow)

    def test_training_reuses_the_autoresearch_data_lane(self) -> None:
        workflow = (ROOT / ".github/workflows/ml-learning.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("group: wickless-autoresearch-nightly", workflow)
        self.assertIn("wickless-autoresearch-data-v2-walk-forward", workflow)
        self.assertIn("wickless_ml/training.request", workflow)
        self.assertIn("python -m wickless_ml.phase3_training", workflow)
        self.assertIn("Train and validate Phase 3 challengers", workflow)

    def test_one_time_integration_files_are_absent(self) -> None:
        forbidden = (
            ".github/workflows/ml-integrate-once.yml",
            ".github/workflows/ml-integration-status-once.yml",
            "scripts/apply_ml_learning_patch.py",
            "scripts/run_ml_learning_patch.py",
            "wickless_ml/integration.request",
            "wickless_ml/integration-status.request",
            "wickless_ml/integration-status.json",
        )
        self.assertEqual(
            [path for path in forbidden if (ROOT / path).exists()],
            [],
        )


if __name__ == "__main__":
    unittest.main()
