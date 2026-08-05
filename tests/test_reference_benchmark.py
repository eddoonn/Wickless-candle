from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autoresearch.reference_benchmark import ensure_reference_benchmark


def sample_report() -> dict:
    metrics = {
        "trades": 2,
        "wins": 0,
        "losses": 2,
        "win_rate": 0.0,
        "net_r": -2.0,
        "expectancy_r": -1.0,
        "profit_factor": 0.0,
        "maximum_drawdown_r": 2.0,
        "distinct_pairs": 2,
        "maximum_pair_trade_share": 0.5,
        "trades_by_pair": {"EURUSD": 1, "USDJPY": 1},
    }
    return {
        "schema_version": 1,
        "candidate": {
            "name": "production-baseline",
            "description": "Production reference.",
            "parameters": {},
            "source_sha256": "a" * 64,
        },
        "folds": {
            "june_2026": {"metrics": metrics},
            "july_2026": {"metrics": {**metrics, "trades": 13, "net_r": 13.0}},
        },
        "overall": {
            **metrics,
            "trades": 15,
            "net_r": 11.0,
            "profit_factor": 2.8,
            "distinct_pairs": 5,
            "maximum_pair_trade_share": 0.53,
            "ambiguous_exits": 0,
        },
        "objective": {
            "worst_fold_net_r": -2.0,
            "total_net_r": 11.0,
            "overall_profit_factor": 2.8,
            "negative_overall_drawdown_r": -2.0,
            "total_trades": 15,
        },
        "acceptance_gates": {
            "passed": False,
            "checks": {"june_2026_minimum_trades": False},
        },
    }


class ReferenceBenchmarkTests(unittest.TestCase):
    def test_missing_incumbent_creates_reference_even_when_gates_fail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch("autoresearch.reference_benchmark.load_policy", return_value={}),
                patch(
                    "autoresearch.reference_benchmark.evaluate",
                    return_value=sample_report(),
                ),
                patch(
                    "autoresearch.reference_benchmark._git_commit",
                    return_value="1" * 40,
                ),
            ):
                payload, created = ensure_reference_benchmark(
                    data_root=root / "data",
                    policy_path=root / "policy.json",
                    ledger_path=root / "results.jsonl",
                    incumbent_path=root / "incumbent.json",
                    runs_path=root / "runs",
                    attempts_path=root / "attempts.log",
                )

            self.assertTrue(created)
            self.assertEqual(payload["benchmark_role"], "production-reference")
            self.assertFalse(payload["report"]["acceptance_gates"]["passed"])
            record = json.loads((root / "results.jsonl").read_text(encoding="utf-8"))
            self.assertEqual(record["status"], "keep")
            self.assertEqual(record["category"], "baseline")
            self.assertTrue((root / "incumbent.json").exists())

    def test_existing_incumbent_is_preserved_without_re_evaluation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            incumbent = root / "incumbent.json"
            incumbent.write_text(
                json.dumps({"schema_version": 1, "report": sample_report()}),
                encoding="utf-8",
            )
            with patch(
                "autoresearch.reference_benchmark.evaluate",
                side_effect=AssertionError("existing incumbent should be reused"),
            ):
                payload, created = ensure_reference_benchmark(
                    data_root=root / "data",
                    policy_path=root / "policy.json",
                    ledger_path=root / "results.jsonl",
                    incumbent_path=incumbent,
                    runs_path=root / "runs",
                    attempts_path=root / "attempts.log",
                )
            self.assertFalse(created)
            self.assertEqual(payload["report"]["candidate"]["name"], "production-baseline")


if __name__ == "__main__":
    unittest.main()
