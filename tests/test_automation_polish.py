from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from autoresearch.behavior import behavior_digest, outcome_digest
from autoresearch.notifications import bootstrap_message, refresh_message
from autoresearch.reference_benchmark import _load_usable_incumbent
from production_session import PRODUCTION_RELEASE_SHA
from scripts.system_health import audit


ROOT = Path(__file__).resolve().parents[1]


def sample_report(name: str = "candidate-a") -> dict:
    metrics = {
        "trades": 1,
        "wins": 1,
        "losses": 0,
        "net_r": 1.9,
        "maximum_drawdown_r": 0.0,
    }
    return {
        "candidate": {
            "name": name,
            "description": name,
            "parameters": {},
            "source_sha256": name,
        },
        "folds": {
            "june_2026": {"metrics": metrics, "counters": {"filled_orders": 1}},
            "july_2026": {"metrics": metrics, "counters": {"filled_orders": 1}},
        },
        "overall": {**metrics, "trades": 2},
        "objective": {},
        "acceptance_gates": {"passed": False, "checks": {}},
        "trades": [{"pair": "EURUSD", "net_r_after_costs": 1.9}],
    }


class BehaviorTests(unittest.TestCase):
    def test_candidate_identity_does_not_change_fingerprints(self) -> None:
        first = sample_report("candidate-a")
        second = sample_report("candidate-b")
        second["candidate"]["parameters"] = {"minimum_body_ratio": 0.82}
        self.assertEqual(behavior_digest(first), behavior_digest(second))
        self.assertEqual(outcome_digest(first), outcome_digest(second))

    def test_counter_change_is_funnel_only(self) -> None:
        first = sample_report()
        second = copy.deepcopy(first)
        second["folds"]["june_2026"]["counters"]["filled_orders"] = 2
        self.assertNotEqual(behavior_digest(first), behavior_digest(second))
        self.assertEqual(outcome_digest(first), outcome_digest(second))

    def test_trade_change_updates_both_fingerprints(self) -> None:
        first = sample_report()
        second = copy.deepcopy(first)
        second["trades"][0]["net_r_after_costs"] = -1.0
        second["overall"]["net_r"] = -1.0
        self.assertNotEqual(behavior_digest(first), behavior_digest(second))
        self.assertNotEqual(outcome_digest(first), outcome_digest(second))


class ReleaseTests(unittest.TestCase):
    def test_stale_incumbent_is_automatically_rejected(self) -> None:
        report = sample_report("production-baseline")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "incumbent.json"
            path.write_text(
                json.dumps(
                    {
                        "production_release_sha": "0" * 40,
                        "report": report,
                    }
                ),
                encoding="utf-8",
            )
            self.assertIsNone(_load_usable_incumbent(path))
            path.write_text(
                json.dumps(
                    {
                        "production_release_sha": PRODUCTION_RELEASE_SHA,
                        "report": report,
                    }
                ),
                encoding="utf-8",
            )
            self.assertIsNotNone(_load_usable_incumbent(path))


class NotificationTests(unittest.TestCase):
    def test_bootstrap_and_refresh_messages_are_bounded(self) -> None:
        bootstrap = bootstrap_message(
            {
                "tested": 100,
                "passing": 0,
                "selected": None,
            }
        )
        refresh = refresh_message(
            {
                "production_release_sha": PRODUCTION_RELEASE_SHA,
                "folds": {
                    "june_2026": {"trades": 3, "net_r": -3.0},
                    "july_2026": {"trades": 16, "net_r": 10.5},
                },
                "overall": {
                    "trades": 19,
                    "net_r": 7.5,
                    "maximum_drawdown_r": 5.2,
                },
            }
        )
        self.assertIn("existing release-current reference", bootstrap)
        self.assertIn(PRODUCTION_RELEASE_SHA[:8], refresh)
        self.assertLess(len(bootstrap), 2000)
        self.assertLess(len(refresh), 2000)


class GuardTests(unittest.TestCase):
    def test_protected_scope_guard_only_targets_autoresearch_experiments(self) -> None:
        workflow = (ROOT / ".github/workflows/autoresearch.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("github.event_name == 'pull_request'", workflow)
        self.assertIn("startsWith(github.head_ref, 'autoresearch/')", workflow)
        self.assertIn("github.head_ref != 'autoresearch/framework-v1'", workflow)
        self.assertNotIn("github.head_ref != 'autoresearch/framework-v1'\n        run:", workflow)


class HealthTests(unittest.TestCase):
    def test_repository_health_has_no_critical_failures(self) -> None:
        report = audit(ROOT)
        self.assertTrue(report["healthy"])
        self.assertEqual(report["failures"], 0)
        checks = {row["name"]: row for row in report["checks"]}
        self.assertEqual(checks["locked_sessions_absent_from_search"]["status"], "pass")
        self.assertEqual(checks["single_flight_live_scans"]["status"], "pass")


if __name__ == "__main__":
    unittest.main()
