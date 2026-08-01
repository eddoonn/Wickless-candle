from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from autoresearch.evaluator import (
    CandidateError,
    candidate_beats,
    load_candidate,
    load_policy,
    validate_parameters,
)
from autoresearch.run_experiment import _append_ledger, _read_ledger
from autoresearch.verify_scope import verify


ROOT = Path(__file__).resolve().parents[1]


class CandidateContractTests(unittest.TestCase):
    def test_baseline_candidate_is_literal_and_empty(self) -> None:
        candidate = load_candidate(ROOT / "autoresearch" / "candidate.py")
        self.assertEqual(candidate.name, "production-baseline")
        self.assertEqual(candidate.parameters, {})

    def test_candidate_cannot_import_or_execute_code(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candidate.py"
            path.write_text(
                "import os\nCANDIDATE = {'name':'bad-one','description':'bad','parameters':{}}\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(CandidateError, "only a module docstring"):
                load_candidate(path)

    def test_protected_safety_field_is_not_exposed(self) -> None:
        with self.assertRaisesRegex(CandidateError, "protected or unknown"):
            validate_parameters({"maximum_cost_to_risk_ratio": 0.2})

    def test_allowed_parameter_is_normalized(self) -> None:
        values = validate_parameters(
            {"minimum_body_ratio": 0.79, "session_start": "05:15"}
        )
        self.assertEqual(values["minimum_body_ratio"], 0.79)
        self.assertEqual(values["session_start"].isoformat(), "05:15:00")


class ObjectiveTests(unittest.TestCase):
    def test_failed_gate_never_beats_incumbent(self) -> None:
        policy = load_policy(ROOT / "autoresearch" / "policy.json")
        incumbent = {
            "objective": {
                "worst_fold_net_r": 1,
                "total_net_r": 2,
                "overall_profit_factor": 2,
                "negative_overall_drawdown_r": -1,
                "total_trades": 20,
            },
            "acceptance_gates": {"passed": True},
        }
        candidate = json.loads(json.dumps(incumbent))
        candidate["objective"]["worst_fold_net_r"] = 99
        candidate["acceptance_gates"]["passed"] = False
        self.assertFalse(candidate_beats(candidate, incumbent, policy))

    def test_worst_fold_profit_is_primary(self) -> None:
        policy = load_policy(ROOT / "autoresearch" / "policy.json")
        incumbent = {
            "objective": {
                "worst_fold_net_r": 1,
                "total_net_r": 20,
                "overall_profit_factor": 4,
                "negative_overall_drawdown_r": -1,
                "total_trades": 30,
            },
            "acceptance_gates": {"passed": True},
        }
        candidate = json.loads(json.dumps(incumbent))
        candidate["objective"]["worst_fold_net_r"] = 1.1
        candidate["objective"]["total_net_r"] = 19
        self.assertTrue(candidate_beats(candidate, incumbent, policy))


class LedgerTests(unittest.TestCase):
    def test_ledger_is_hash_chained_and_tamper_evident(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "results.jsonl"
            first = _append_ledger(path, {"run_id": "one"})
            second = _append_ledger(path, {"run_id": "two"})
            records, head = _read_ledger(path)
            self.assertEqual(len(records), 2)
            self.assertEqual(second["previous_sha256"], first["record_sha256"])
            self.assertEqual(head, second["record_sha256"])
            path.write_text(
                path.read_text(encoding="utf-8").replace('"run_id":"one"', '"run_id":"tampered"'),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "digest mismatch"):
                _read_ledger(path)


class ScopeTests(unittest.TestCase):
    def test_only_candidate_and_audit_artifacts_are_allowed(self) -> None:
        self.assertEqual(
            verify(
                [
                    "autoresearch/candidate.py",
                    "autoresearch/results.jsonl",
                    "autoresearch/runs/abc.json",
                ]
            ),
            [],
        )
        self.assertEqual(verify(["no_wick_research.py"]), ["no_wick_research.py"])


if __name__ == "__main__":
    unittest.main()

