from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import scripts.scanner_health as scanner_health
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

    def test_heartbeat_delay_alone_does_not_degrade_state(self) -> None:
        # The heartbeat starting late is GitHub scheduler delay, not a scanner
        # problem, so it must not force DEGRADED on an otherwise healthy day.
        now = datetime(2026, 8, 5, 19, 5, 14, tzinfo=UTC)
        checkpoint = datetime(2026, 8, 5, 17, 45, tzinfo=UTC)
        runs = [run(index, now - timedelta(minutes=5 * index)) for index in range(100)]
        report = evaluate_runs(runs, now=now, checkpoint=checkpoint)
        self.assertEqual(report["state"], "HEALTHY")
        self.assertFalse(any("heartbeat started" in reason for reason in report["reasons"]))

    def test_rebaselined_cadence_accepts_sparse_scheduler_days(self) -> None:
        # GitHub's best-effort scheduler delivers roughly 5-12 runs/day for
        # this workflow, so a fresh successful scan is healthy even far below
        # the */5 nominal cadence of 288/day.
        now = datetime(2026, 8, 5, 17, 50, tzinfo=UTC)
        checkpoint = datetime(2026, 8, 5, 17, 45, tzinfo=UTC)
        runs = [run(200 + index, now - timedelta(hours=4 * index)) for index in range(6)]
        report = evaluate_runs(runs, now=now, checkpoint=checkpoint)
        self.assertEqual(report["state"], "HEALTHY")
        self.assertFalse(report["needs_recovery"])

    def test_cadence_below_rebaselined_floor_is_degraded(self) -> None:
        now = datetime(2026, 8, 5, 17, 50, tzinfo=UTC)
        checkpoint = datetime(2026, 8, 5, 17, 45, tzinfo=UTC)
        runs = [run(300, now), run(301, now - timedelta(hours=10))]
        report = evaluate_runs(runs, now=now, checkpoint=checkpoint)
        self.assertEqual(report["state"], "DEGRADED")
        self.assertFalse(report["needs_recovery"])

    def test_active_scan_never_forces_unhealthy(self) -> None:
        # A queued or in-progress scan means the scanner is alive even when
        # the last completed run is old; the recovery race that previously
        # forced UNHEALTHY must not resurface.
        now = datetime(2026, 8, 5, 19, 50, 49, tzinfo=UTC)
        checkpoint = datetime(2026, 8, 5, 17, 45, tzinfo=UTC)
        runs = [
            run(400, now - timedelta(minutes=1), status="in_progress", conclusion=None),
            run(401, now - timedelta(minutes=130), conclusion="success"),
        ]
        report = evaluate_runs(runs, now=now, checkpoint=checkpoint)
        self.assertFalse(report["needs_recovery"])
        self.assertNotEqual(report["state"], "UNHEALTHY")

    def test_wait_for_recovery_accepts_later_success_after_cancellation(self) -> None:
        cancelled = run(
            500,
            datetime(2026, 8, 5, 19, 0, tzinfo=UTC),
            conclusion="cancelled",
            event="workflow_dispatch",
        )
        succeeded = run(
            501,
            datetime(2026, 8, 5, 19, 1, tzinfo=UTC),
            conclusion="success",
            event="schedule",
        )
        calls = {"count": 0}

        def fake_fetch(repository, token, workflow):
            calls["count"] += 1
            return [cancelled] if calls["count"] == 1 else [succeeded]

        original_fetch = scanner_health.fetch_runs
        original_poll = scanner_health.POLL_SECONDS
        scanner_health.fetch_runs = fake_fetch
        scanner_health.POLL_SECONDS = 0
        try:
            result = scanner_health.wait_for_recovery(
                "owner/repo",
                "token",
                "live-signals.yml",
                before_ids={499},
                existing_active_id=None,
                timeout_seconds=5,
            )
        finally:
            scanner_health.fetch_runs = original_fetch
            scanner_health.POLL_SECONDS = original_poll
        self.assertIsNotNone(result)
        self.assertTrue(result["recovered"])
        self.assertEqual(int(result["run"]["id"]), 501)

    def test_wait_for_recovery_reports_failure_after_only_cancellations(self) -> None:
        cancelled = run(
            510,
            datetime(2026, 8, 5, 19, 0, tzinfo=UTC),
            conclusion="cancelled",
            event="workflow_dispatch",
        )

        def fake_fetch(repository, token, workflow):
            return [cancelled]

        original_fetch = scanner_health.fetch_runs
        original_poll = scanner_health.POLL_SECONDS
        scanner_health.fetch_runs = fake_fetch
        scanner_health.POLL_SECONDS = 0
        try:
            result = scanner_health.wait_for_recovery(
                "owner/repo",
                "token",
                "live-signals.yml",
                before_ids={509},
                existing_active_id=None,
                timeout_seconds=1,
            )
        finally:
            scanner_health.fetch_runs = original_fetch
            scanner_health.POLL_SECONDS = original_poll
        self.assertIsNotNone(result)
        self.assertFalse(result["recovered"])
        self.assertEqual(int(result["run"]["id"]), 510)

    def test_wait_for_recovery_returns_none_when_nothing_completes(self) -> None:
        running = run(
            520,
            datetime(2026, 8, 5, 19, 0, tzinfo=UTC),
            status="in_progress",
            conclusion=None,
        )

        def fake_fetch(repository, token, workflow):
            return [running]

        original_fetch = scanner_health.fetch_runs
        original_poll = scanner_health.POLL_SECONDS
        scanner_health.fetch_runs = fake_fetch
        scanner_health.POLL_SECONDS = 0
        try:
            result = scanner_health.wait_for_recovery(
                "owner/repo",
                "token",
                "live-signals.yml",
                before_ids={519},
                existing_active_id=None,
                timeout_seconds=1,
            )
        finally:
            scanner_health.fetch_runs = original_fetch
            scanner_health.POLL_SECONDS = original_poll
        self.assertIsNone(result)

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
