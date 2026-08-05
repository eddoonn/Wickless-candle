from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from autoresearch.attempts import idea_category
from autoresearch.evaluator import load_candidate
from autoresearch.nightly_batch import (
    Proposal,
    discord_summary,
    parameter_signature,
    proposal_space,
    render_candidate,
    select_best_experiment,
    select_proposals,
)


ROOT = Path(__file__).resolve().parents[1]


class ProposalTests(unittest.TestCase):
    def test_workflow_runs_at_eleven_pm_london_and_pushes_only_nightly(self) -> None:
        workflow = (ROOT / ".github/workflows/autoresearch-nightly.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn('cron: "0 23 * * *"', workflow)
        self.assertIn('timezone: "Europe/London"', workflow)
        self.assertIn('NIGHTLY_BRANCH: autoresearch/nightly', workflow)
        self.assertIn('git push origin "$NIGHTLY_BRANCH"', workflow)
        self.assertIn("default: \"12\"", workflow)
        self.assertIn("--coach-interval 20", workflow)
        self.assertNotIn("git push origin main", workflow)

    def test_space_is_large_deterministic_and_unique(self) -> None:
        first = proposal_space()
        second = proposal_space()
        self.assertEqual(first, second)
        self.assertGreater(len(first), 500)
        signatures = [parameter_signature(row.parameters) for row in first]
        self.assertEqual(len(signatures), len(set(signatures)))
        self.assertTrue(all(1 <= len(row.parameters) <= 2 for row in first))

    def test_selection_skips_parameters_already_in_ledger(self) -> None:
        first = proposal_space()[0]
        record = {
            "candidate": {"parameters": first.parameters},
            "previous_sha256": "0" * 64,
        }
        encoded = json.dumps(record, sort_keys=True, separators=(",", ":"))
        import hashlib

        record["record_sha256"] = hashlib.sha256(encoded.encode()).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "results.jsonl"
            ledger.write_text(
                json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            selected = select_proposals(ledger, 2)
        self.assertEqual(selected, proposal_space()[1:3])

    def test_selection_obeys_playbook_priorities_and_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = root / "results.jsonl"
            playbook = root / "playbook.md"
            playbook.write_text(
                "# Test playbook\n\n"
                "## Explore next\n"
                "- trend-filter: Explore EMA behavior.\n\n"
                "## Do not try\n"
                "- candle-quality: exhausted.\n",
                encoding="utf-8",
            )
            selected = select_proposals(ledger, 1, playbook)
        self.assertEqual(len(selected), 1)
        self.assertEqual(idea_category(selected[0].parameters), "trend-filter")

    def test_rendered_candidate_passes_literal_contract(self) -> None:
        proposal = Proposal(
            name="test-candidate",
            description="A small controlled test.",
            parameters={"minimum_body_ratio": 0.79, "session_start": "04:45"},
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candidate.py"
            path.write_text(render_candidate(proposal), encoding="utf-8")
            loaded = load_candidate(path)
        self.assertEqual(loaded.name, proposal.name)
        self.assertEqual(loaded.parameters["minimum_body_ratio"], 0.79)
        self.assertEqual(loaded.parameters["session_start"].isoformat(), "04:45:00")

    def test_best_experiment_uses_the_policy_objective_order(self) -> None:
        policy = {
            "objective_order": [
                "worst_fold_net_r",
                "total_net_r",
                "overall_profit_factor",
                "negative_overall_drawdown_r",
                "total_trades",
            ]
        }
        high_total_worse_fold = {
            "candidate": "high-total",
            "objective": {
                "worst_fold_net_r": -3.0,
                "total_net_r": 20.0,
                "overall_profit_factor": 4.0,
                "negative_overall_drawdown_r": -3.0,
                "total_trades": 20,
            },
        }
        stronger_worst_fold = {
            "candidate": "stronger-fold",
            "objective": {
                "worst_fold_net_r": -2.0,
                "total_net_r": 12.0,
                "overall_profit_factor": 3.0,
                "negative_overall_drawdown_r": -2.0,
                "total_trades": 15,
            },
        }
        selected = select_best_experiment(
            [high_total_worse_fold, stronger_worst_fold], policy
        )
        self.assertEqual(selected["candidate"], "stronger-fold")

    def test_discord_summary_compares_benchmark_with_best_experiment(self) -> None:
        benchmark_june = {
            "trades": 2,
            "net_r": -2.06,
            "maximum_drawdown_r": 2.06,
        }
        benchmark_july = {
            "trades": 13,
            "net_r": 13.68,
            "maximum_drawdown_r": 2.06,
        }
        benchmark_overall = {
            "trades": 15,
            "net_r": 11.62,
            "maximum_drawdown_r": 2.14,
        }
        best_june = {"trades": 2, "net_r": -2.06, "maximum_drawdown_r": 2.06}
        best_july = {"trades": 14, "net_r": 15.65, "maximum_drawdown_r": 2.06}
        best_overall = {"trades": 16, "net_r": 13.59, "maximum_drawdown_r": 2.06}
        summary = {
            "london_date": "2026-08-01",
            "tested": 12,
            "kept": 0,
            "rejected": 12,
            "benchmark": {
                "name": "production-baseline",
                "overall": benchmark_overall,
                "folds": {
                    "june_2026": benchmark_june,
                    "july_2026": benchmark_july,
                },
            },
            "best_experiment": {
                "candidate": "grid-0031",
                "status": "discard",
                "overall": best_overall,
                "folds": {"june_2026": best_june, "july_2026": best_july},
                "acceptance_gates": {
                    "checks": {"june_2026_positive_net_r": False}
                },
            },
            "incumbent": {
                "name": "production-baseline",
                "overall": benchmark_overall,
                "folds": {
                    "june_2026": benchmark_june,
                    "july_2026": benchmark_july,
                },
            },
        }
        message = discord_summary(summary)
        self.assertIn("KEEP **0**", message)
        self.assertIn("Benchmark — production-baseline", message)
        self.assertIn("Best tonight — grid-0031", message)
        self.assertIn("Overall +1.97R", message)
        self.assertIn("Decision: **DISCARD**", message)
        self.assertIn("june 2026 positive net r", message)
        self.assertIn("autoresearch/nightly", message)
        self.assertLess(len(message), 2000)


if __name__ == "__main__":
    unittest.main()
