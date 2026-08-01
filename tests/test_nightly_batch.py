from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from autoresearch.evaluator import load_candidate
from autoresearch.nightly_batch import (
    Proposal,
    discord_summary,
    parameter_signature,
    proposal_space,
    render_candidate,
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

    def test_discord_summary_is_concise(self) -> None:
        metric = {"trades": 12, "net_r": 4.25, "maximum_drawdown_r": 1.1}
        summary = {
            "london_date": "2026-08-01",
            "tested": 12,
            "kept": 1,
            "rejected": 11,
            "incumbent": {
                "name": "grid-0001",
                "overall": metric,
                "folds": {"june_2026": metric, "july_2026": metric},
            },
        }
        message = discord_summary(summary)
        self.assertIn("KEEP **1**", message)
        self.assertIn("June: 12 trades, +4.25R", message)
        self.assertIn("autoresearch/nightly", message)
        self.assertLess(len(message), 2000)


if __name__ == "__main__":
    unittest.main()
