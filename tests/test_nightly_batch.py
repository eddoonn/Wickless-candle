from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from autoresearch.attempts import idea_category
from autoresearch.evaluator import load_candidate
from autoresearch.nightly_batch import (
    LOCKED_SESSION_PARAMETERS,
    Proposal,
    discord_summary,
    parameter_signature,
    proposal_family,
    proposal_space,
    render_candidate,
    select_best_experiment,
    select_proposals,
)


ROOT = Path(__file__).resolve().parents[1]


class ProposalTests(unittest.TestCase):
    def test_workflow_runs_at_eleven_pm_london_and_publishes_scoped_state_to_main(self) -> None:
        workflow = (ROOT / ".github/workflows/autoresearch-nightly.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn('cron: "0 23 * * *"', workflow)
        self.assertIn('timezone: "Europe/London"', workflow)
        self.assertIn('MAIN_BRANCH: main', workflow)
        self.assertIn('git push origin HEAD:"$MAIN_BRANCH"', workflow)
        self.assertIn('default: "12"', workflow)
        self.assertIn("--coach-interval 20", workflow)
        self.assertIn("refresh_baseline:", workflow)
        self.assertIn("bootstrap_benchmark:", workflow)
        self.assertIn("Ensure production reference benchmark", workflow)
        self.assertIn("Bootstrap the strongest valid benchmark", workflow)
        self.assertIn("Run worker and coach experiment loop", workflow)
        self.assertIn("--no-discord", workflow)
        self.assertNotIn("--git-commits", workflow)
        self.assertIn("Validate code, health, and changed-file scope", workflow)
        self.assertIn("Publish audit history to main", workflow)
        self.assertLess(
            workflow.index("Upload durable autoresearch audit artifact"),
            workflow.index("Publish audit history to main"),
        )
        self.assertLess(
            workflow.index("Publish audit history to main"),
            workflow.index("Notify Discord of nightly benchmark and best test"),
        )
        self.assertIn("autoresearch/nightly-run.request", workflow)
        self.assertNotIn("FRAMEWORK_BRANCH:", workflow)
        self.assertNotIn("NIGHTLY_BRANCH:", workflow)
        self.assertNotIn("ref: autoresearch/framework-v1", workflow)
        self.assertNotIn('git push origin "$NIGHTLY_BRANCH"', workflow)

        policy = json.loads((ROOT / "autoresearch/policy.json").read_text(encoding="utf-8"))
        self.assertEqual(policy["repository_branch"], "main")
        folds = {fold["name"]: fold for fold in policy["folds"]}
        self.assertEqual(folds["june_2026"]["minimum_trades"], 10)
        self.assertEqual(folds["july_2026"]["minimum_trades"], 10)
        self.assertLess(policy["acceptance"]["minimum_net_r_each_fold"], -1e8)

    def test_space_is_large_deterministic_unique_and_has_no_clock_mutations(self) -> None:
        first = proposal_space()
        second = proposal_space()
        self.assertEqual(first, second)
        self.assertGreater(len(first), 500)
        signatures = [parameter_signature(row.parameters) for row in first]
        self.assertEqual(len(signatures), len(set(signatures)))
        self.assertTrue(
            all(not LOCKED_SESSION_PARAMETERS.intersection(row.parameters) for row in first)
        )
        self.assertTrue(any(row.parameters.get("entry_model") for row in first))

    def test_selection_skips_parameters_already_in_ledger(self) -> None:
        first = proposal_space()[0]
        record = {
            "candidate": {"parameters": first.parameters},
            "previous_sha256": "0" * 64,
        }
        encoded = json.dumps(record, sort_keys=True, separators=(",", ":"))
        record["record_sha256"] = hashlib.sha256(encoded.encode()).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = root / "results.jsonl"
            playbook = root / "playbook.md"
            ledger.write_text(
                json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            playbook.write_text("", encoding="utf-8")
            selected = select_proposals(ledger, 4, playbook)
        self.assertEqual(len(selected), 4)
        self.assertNotIn(parameter_signature(first.parameters), {
            parameter_signature(row.parameters) for row in selected
        })

    def test_selection_diversifies_categories_and_parameter_families(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            selected = select_proposals(root / "results.jsonl", 8, root / "playbook.md")
        categories = {idea_category(row.parameters) for row in selected}
        families = {proposal_family(row.parameters) for row in selected}
        self.assertGreaterEqual(len(categories), 4)
        self.assertGreaterEqual(len(families), 6)

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
            parameters={"minimum_body_ratio": 0.79, "trend_filter": "none"},
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candidate.py"
            path.write_text(render_candidate(proposal), encoding="utf-8")
            loaded = load_candidate(path)
        self.assertEqual(loaded.name, proposal.name)
        self.assertEqual(loaded.parameters["minimum_body_ratio"], 0.79)
        self.assertEqual(loaded.parameters["trend_filter"], "none")

    def test_best_experiment_prioritizes_gate_and_trade_progress(self) -> None:
        policy = {
            "objective_order": [
                "worst_fold_net_r",
                "total_net_r",
                "overall_profit_factor",
                "negative_overall_drawdown_r",
                "total_trades",
            ]
        }

        def row(
            name: str,
            effect: str,
            *,
            passed_gates: int,
            june: int,
            july: int,
            total: int,
            worst: float,
            total_net: float,
        ) -> dict:
            return {
                "candidate": name,
                "effect": effect,
                "acceptance_gates": {
                    "checks": {
                        f"gate_{index}": index < passed_gates
                        for index in range(10)
                    }
                },
                "folds": {
                    "june_2026": {"trades": june},
                    "july_2026": {"trades": july},
                },
                "overall": {"trades": total},
                "objective": {
                    "worst_fold_net_r": worst,
                    "total_net_r": total_net,
                    "overall_profit_factor": 2.0,
                    "negative_overall_drawdown_r": -2.0,
                    "total_trades": total,
                },
            }

        no_effect = row(
            "identical",
            "no-effect",
            passed_gates=10,
            june=99,
            july=99,
            total=198,
            worst=99.0,
            total_net=198.0,
        )
        funnel_only = row(
            "funnel-only",
            "funnel-only",
            passed_gates=10,
            june=99,
            july=99,
            total=198,
            worst=100.0,
            total_net=200.0,
        )
        degenerate = row(
            "zero-trade",
            "trade-changed",
            passed_gates=5,
            june=0,
            july=0,
            total=0,
            worst=0.0,
            total_net=0.0,
        )
        lower_coverage = row(
            "lower-coverage",
            "trade-changed",
            passed_gates=8,
            june=3,
            july=15,
            total=18,
            worst=-2.0,
            total_net=10.0,
        )
        closer_to_monthly_gate = row(
            "closer-to-monthly-gate",
            "trade-changed",
            passed_gates=8,
            june=4,
            july=14,
            total=18,
            worst=-3.0,
            total_net=8.0,
        )
        selected = select_best_experiment(
            [
                no_effect,
                funnel_only,
                degenerate,
                lower_coverage,
                closer_to_monthly_gate,
            ],
            policy,
        )
        self.assertEqual(selected["candidate"], "closer-to-monthly-gate")
        self.assertIsNone(select_best_experiment([no_effect, funnel_only], policy))

    def test_discord_summary_reports_trade_funnel_and_no_effect_counts(self) -> None:
        benchmark_june = {"trades": 3, "net_r": -3.09, "maximum_drawdown_r": 3.09}
        benchmark_july = {"trades": 16, "net_r": 10.58, "maximum_drawdown_r": 3.09}
        benchmark_overall = {"trades": 19, "net_r": 7.49, "maximum_drawdown_r": 5.22}
        best_june = {"trades": 4, "net_r": -2.00, "maximum_drawdown_r": 3.00}
        best_july = {"trades": 17, "net_r": 11.50, "maximum_drawdown_r": 3.00}
        best_overall = {"trades": 21, "net_r": 9.50, "maximum_drawdown_r": 4.50}
        summary = {
            "london_date": "2026-08-05",
            "tested": 12,
            "trade_changed": 5,
            "funnel_only": 2,
            "no_effect": 5,
            "kept": 0,
            "rejected": 12,
            "category_counts": {"candle-quality": 3, "trend-filter": 3},
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
                "parameters": {"ema_length": 45},
                "status": "discard",
                "effect": "trade-changed",
                "overall": best_overall,
                "folds": {"june_2026": best_june, "july_2026": best_july},
                "acceptance_gates": {
                    "checks": {"june_2026_minimum_trades": False}
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
            "coach_runs": [],
        }
        message = discord_summary(summary)
        self.assertIn("Trade changed **5**", message)
        self.assertIn("Funnel only **2**", message)
        self.assertIn("No effect **5**", message)
        self.assertIn("Best trade-changing test — grid-0031", message)
        self.assertIn("ema_length=45", message)
        self.assertIn("Decision: **DISCARD**", message)
        self.assertLess(len(message), 2000)


if __name__ == "__main__":
    unittest.main()
