from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from autoresearch.attempts import (
    append_attempt,
    format_score,
    read_attempts,
    sync_attempts_from_ledger,
)
from autoresearch.coach import run_coach_if_due
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

    def test_allowed_parameters_are_normalized(self) -> None:
        values = validate_parameters(
            {"minimum_body_ratio": 0.79, "trend_filter": "none"}
        )
        self.assertEqual(values["minimum_body_ratio"], 0.79)
        self.assertEqual(values["trend_filter"], "none")


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


class AttemptLogTests(unittest.TestCase):
    def test_attempt_log_is_one_append_only_line_per_result(self) -> None:
        objective = {
            "worst_fold_net_r": 1.0,
            "total_net_r": 2.0,
            "overall_profit_factor": 1.5,
            "negative_overall_drawdown_r": -0.5,
            "total_trades": 12,
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "attempts.log"
            append_attempt(
                path,
                timestamp="2026-08-01T00:00:00+00:00",
                description="Tested a body filter.",
                category="candle-quality",
                score=format_score(objective),
                status="discard",
            )
            first = path.read_bytes()
            append_attempt(
                path,
                timestamp="2026-08-01T00:01:00+00:00",
                description="Tested an EMA filter.",
                category="trend-filter",
                score=format_score(objective),
                status="keep",
            )
            self.assertTrue(path.read_bytes().startswith(first))
            attempts = read_attempts(path)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0].decision, "DISCARDED")
        self.assertEqual(attempts[1].decision, "KEPT")

    def test_historical_session_category_remains_readable(self) -> None:
        objective = {
            "worst_fold_net_r": 0.0,
            "total_net_r": 1.0,
            "overall_profit_factor": 1.5,
            "negative_overall_drawdown_r": -1.0,
            "total_trades": 11,
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "attempts.log"
            append_attempt(
                path,
                timestamp="2026-08-01T00:00:00+00:00",
                description="Historical session test.",
                category="session-window",
                score=format_score(objective),
                status="discard",
            )
            attempts = read_attempts(path)
        self.assertEqual(attempts[0].category, "session-window")

    def test_attempt_log_backfills_and_is_verified_against_ledger(self) -> None:
        record = {
            "generated_at_utc": "2026-08-01T00:00:00+00:00",
            "candidate": {
                "name": "grid-0001",
                "description": "Tested a body filter.",
                "parameters": {"minimum_body_ratio": 0.82},
            },
            "objective": {
                "worst_fold_net_r": 1.0,
                "total_net_r": 2.0,
                "overall_profit_factor": 1.5,
                "negative_overall_drawdown_r": -0.5,
                "total_trades": 12,
            },
            "status": "discard",
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "attempts.log"
            self.assertEqual(sync_attempts_from_ledger(path, [record]), 1)
            self.assertEqual(sync_attempts_from_ledger(path, [record]), 0)
            path.write_text(
                path.read_text(encoding="utf-8").replace("body filter", "EMA filter"),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "does not match"):
                sync_attempts_from_ledger(path, [record])


class CoachTests(unittest.TestCase):
    @staticmethod
    def _write_attempts(path: Path, categories: list[str], kept_index: int | None = None) -> None:
        objective = {
            "worst_fold_net_r": 0.0,
            "total_net_r": 1.0,
            "overall_profit_factor": 1.5,
            "negative_overall_drawdown_r": -1.0,
            "total_trades": 11,
        }
        for index, category in enumerate(categories):
            append_attempt(
                path,
                timestamp=f"2026-08-01T00:00:{index:02d}+00:00",
                description=f"Attempt {index} in {category}.",
                category=category,
                score=format_score(objective),
                status="keep" if index == kept_index else "discard",
            )

    def test_flatline_rewrites_playbook_and_marks_exhausted_categories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            attempts = root / "attempts.log"
            playbook = root / "playbook.md"
            state = root / "coach_state.json"
            playbook.write_text(
                (ROOT / "autoresearch" / "playbook.md").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            self._write_attempts(
                attempts,
                ["candle-quality"] * 10
                + ["trend-filter"] * 5
                + ["entry-model"] * 5,
            )
            result = run_coach_if_due(
                attempts_path=attempts,
                playbook_path=playbook,
                state_path=state,
                interval=20,
            )
            rendered = playbook.read_text(encoding="utf-8")
        self.assertTrue(result.ran)
        self.assertTrue(result.changed)
        self.assertFalse(result.last_ten_improved)
        self.assertEqual(result.playbook_priorities, ["wick-detection"])
        self.assertIn("candle-quality: 10 attempts and zero kept improvements", rendered)
        self.assertLessEqual(len(rendered.splitlines()), 40)

    def test_baseline_record_does_not_count_toward_coach_interval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            attempts = root / "attempts.log"
            playbook = root / "playbook.md"
            state = root / "coach_state.json"
            playbook.write_text(
                (ROOT / "autoresearch" / "playbook.md").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            self._write_attempts(attempts, ["baseline"] + ["candle-quality"] * 19)
            result = run_coach_if_due(
                attempts_path=attempts,
                playbook_path=playbook,
                state_path=state,
                interval=20,
            )
        self.assertFalse(result.ran)
        self.assertEqual(result.attempt_count, 19)

    def test_progress_leaves_playbook_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            attempts = root / "attempts.log"
            playbook = root / "playbook.md"
            state = root / "coach_state.json"
            original = (ROOT / "autoresearch" / "playbook.md").read_text(
                encoding="utf-8"
            )
            playbook.write_text(original, encoding="utf-8")
            self._write_attempts(attempts, ["candle-quality"] * 20, kept_index=19)
            result = run_coach_if_due(
                attempts_path=attempts,
                playbook_path=playbook,
                state_path=state,
                interval=20,
            )
            current = playbook.read_text(encoding="utf-8")
        self.assertTrue(result.ran)
        self.assertFalse(result.changed)
        self.assertTrue(result.last_ten_improved)
        self.assertEqual(current, original)


class ScopeTests(unittest.TestCase):
    def test_only_candidate_and_audit_artifacts_are_allowed(self) -> None:
        self.assertEqual(
            verify(
                [
                    "autoresearch/candidate.py",
                    "autoresearch/results.jsonl",
                    "autoresearch/attempts.log",
                    "autoresearch/playbook.md",
                    "autoresearch/coach_state.json",
                    "autoresearch/runs/abc.json",
                ]
            ),
            [],
        )
        self.assertEqual(verify(["no_wick_research.py"]), ["no_wick_research.py"])


if __name__ == "__main__":
    unittest.main()
