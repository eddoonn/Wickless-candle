from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts.scanner_health import evaluate_runs, latest_scheduled_checkpoint


UTC = timezone.utc
ROOT = Path(__file__).resolve().parents[1]


def run(
    identifier: int,
    created_at: datetime,
    *,
    status: str = "completed",
    conclusion: str | None = "success",
    event: str = "schedule",
) -> dict[str, object]:
    return {
        "id": identifier,
        "created_at": created_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "status": status,
        "conclusion": conclusion,
        "event": event,
        "html_url": f"https://github.test/actions/runs/{identifier}",
    }


class ScannerHealthTests(unittest.TestCase):
    def test_delayed_heartbeat_uses_intended_london_checkpoint(self) -> None:
        now = datetime(2026, 8, 5, 19, 5, 14, tzinfo=UTC)
        checkpoint = latest_scheduled_checkpoint(now, event_name="schedule")
        self.assertEqual(checkpoint, datetime(2026, 8, 5, 17, 45, tzinfo=UTC))
        runs = [
            run(index, datetime(2026, 8, 5, 17, 57, tzinfo=UTC) - timedelta(hours=index))
            for index in range(12)
        ]
        report = evaluate_runs(runs, now=now, checkpoint=checkpoint)
        self.assertTrue(report["checkpoint_covered"])
        self.assertTrue(report["needs_recovery"])
        self.assertEqual(report["state"], "UNHEALTHY")
        self.assertAlmostEqual(report["heartbeat_delay_minutes"], 80.2, places=1)

    def test_successful_recovery_becomes_degraded_not_healthy(self) -> None:
        now = datetime(2026, 8, 5, 19, 5, 14, tzinfo=UTC)
        checkpoint = datetime(2026, 8, 5, 17, 45, tzinfo=UTC)
        runs = [
            run(100, datetime(2026, 8, 5, 19, 4, tzinfo=UTC), event="workflow_dispatch"),
            run(99, datetime(2026, 8, 5, 17, 57, tzinfo=UTC)),
        ]
        report = evaluate_runs(runs, now=now, checkpoint=checkpoint)
        self.assertFalse(report["needs_recovery"])
        self.assertEqual(report["state"], "DEGRADED")
        self.assertTrue(report["checkpoint_covered"])

    def test_timely_checkpoint_with_sufficient_cadence_is_healthy(self) -> None:
        now = datetime(2026, 8, 5, 17, 50, tzinfo=UTC)
        checkpoint = datetime(2026, 8, 5, 17, 45, tzinfo=UTC)
        runs = [run(index, now - timedelta(minutes=5 * index)) for index in range(100)]
        report = evaluate_runs(runs, now=now, checkpoint=checkpoint)
        self.assertEqual(report["state"], "HEALTHY")
        self.assertFalse(report["needs_recovery"])
        self.assertTrue(report["checkpoint_covered"])

    def test_manual_dispatch_uses_current_time_as_checkpoint(self) -> None:
        now = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
        self.assertEqual(
            latest_scheduled_checkpoint(now, event_name="workflow_dispatch"),
            now,
        )

    def test_workflow_can_dispatch_one_recovery_scan_without_content_write(self) -> None:
        workflow = (ROOT / ".github/workflows/scanner-health.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("actions: write", workflow)
        self.assertIn("contents: read", workflow)
        self.assertNotIn("contents: write", workflow)
        self.assertIn("python scripts/scanner_health.py", workflow)
        self.assertIn('--event-name "$GITHUB_EVENT_NAME"', workflow)
        self.assertIn("--workflow live-signals.yml", workflow)
        self.assertNotIn("latest_age_minutes <= 20", workflow)


if __name__ == "__main__":
    unittest.main()
