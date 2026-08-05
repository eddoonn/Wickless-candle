#!/usr/bin/env python3
"""Assess live-scanner health against its intended checkpoint and recover stale scans."""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, time as clock_time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


UTC = timezone.utc
LONDON = ZoneInfo("Europe/London")
NOMINAL_RUNS_24H = 24 * 12
MINIMUM_HEALTHY_RUNS_24H = 72
FRESHNESS_MINUTES = 20


def parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def latest_scheduled_checkpoint(
    now: datetime,
    *,
    event_name: str,
    hour: int = 18,
    minute: int = 45,
) -> datetime:
    """Return the intended weekday checkpoint, or now for a manual run."""

    now = now.astimezone(UTC)
    if event_name != "schedule":
        return now
    local_now = now.astimezone(LONDON)
    candidate = datetime.combine(
        local_now.date(),
        clock_time(hour=hour, minute=minute),
        tzinfo=LONDON,
    )
    if local_now < candidate or candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
        while candidate.weekday() >= 5:
            candidate -= timedelta(days=1)
    return candidate.astimezone(UTC)


def _sorted_runs(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(runs, key=lambda row: parse_timestamp(row["created_at"]), reverse=True)


def evaluate_runs(
    runs: list[dict[str, Any]],
    *,
    now: datetime,
    checkpoint: datetime,
    freshness_minutes: int = FRESHNESS_MINUTES,
    minimum_runs_24h: int = MINIMUM_HEALTHY_RUNS_24H,
) -> dict[str, Any]:
    """Classify a run-history snapshot without performing network operations."""

    now = now.astimezone(UTC)
    checkpoint = checkpoint.astimezone(UTC)
    ordered = _sorted_runs(runs)
    cutoff = now - timedelta(hours=24)
    recent = [row for row in ordered if parse_timestamp(row["created_at"]) >= cutoff]
    successful = [
        row
        for row in ordered
        if row.get("status") == "completed" and row.get("conclusion") == "success"
    ]
    active = [
        row
        for row in ordered
        if row.get("status") in {"queued", "in_progress", "waiting", "pending"}
    ]
    latest = ordered[0] if ordered else None
    latest_success = successful[0] if successful else None
    latest_active = active[0] if active else None
    freshness = timedelta(minutes=freshness_minutes)
    current_fresh = bool(
        latest_success and now - parse_timestamp(latest_success["created_at"]) <= freshness
    )
    recent_active = bool(
        latest_active and now - parse_timestamp(latest_active["created_at"]) <= freshness
    )
    checkpoint_candidates = [
        row
        for row in successful
        if abs(parse_timestamp(row["created_at"]) - checkpoint) <= freshness
    ]
    checkpoint_run = (
        min(
            checkpoint_candidates,
            key=lambda row: abs(parse_timestamp(row["created_at"]) - checkpoint),
        )
        if checkpoint_candidates
        else None
    )
    checkpoint_covered = checkpoint_run is not None
    successful_recent = sum(row.get("conclusion") == "success" for row in recent)
    failed_recent = sum(row.get("conclusion") == "failure" for row in recent)
    cancelled_recent = sum(row.get("conclusion") == "cancelled" for row in recent)
    delay_minutes = max(0.0, (now - checkpoint).total_seconds() / 60)
    reasons: list[str] = []
    if not current_fresh and not recent_active:
        reasons.append("no successful or active scan within the last 20 minutes")
    if not checkpoint_covered:
        reasons.append("no successful scan within 20 minutes of the intended checkpoint")
    if len(recent) < minimum_runs_24h:
        reasons.append(
            f"only {len(recent)} scanner runs in 24 hours; healthy minimum is {minimum_runs_24h}"
        )
    if failed_recent:
        reasons.append(f"{failed_recent} scanner run(s) failed in the last 24 hours")
    if cancelled_recent:
        reasons.append(f"{cancelled_recent} scanner run(s) were cancelled in the last 24 hours")
    if delay_minutes > freshness_minutes:
        reasons.append(f"heartbeat started {delay_minutes:.1f} minutes after its checkpoint")

    needs_recovery = not current_fresh and not recent_active
    if needs_recovery:
        state = "UNHEALTHY"
    elif reasons:
        state = "DEGRADED"
    else:
        state = "HEALTHY"

    def age_minutes(row: dict[str, Any] | None) -> float | None:
        if row is None:
            return None
        return round((now - parse_timestamp(row["created_at"])).total_seconds() / 60, 1)

    return {
        "schema_version": 2,
        "generated_at_utc": now.replace(microsecond=0).isoformat(),
        "generated_at_london": now.astimezone(LONDON).replace(microsecond=0).isoformat(),
        "checkpoint_utc": checkpoint.replace(microsecond=0).isoformat(),
        "checkpoint_london": checkpoint.astimezone(LONDON).replace(microsecond=0).isoformat(),
        "heartbeat_delay_minutes": round(delay_minutes, 1),
        "state": state,
        "healthy": state == "HEALTHY",
        "needs_recovery": needs_recovery,
        "reasons": reasons,
        "latest_run": None if latest is None else {**latest, "age_minutes": age_minutes(latest)},
        "latest_success": (
            None
            if latest_success is None
            else {**latest_success, "age_minutes": age_minutes(latest_success)}
        ),
        "latest_active": (
            None
            if latest_active is None
            else {**latest_active, "age_minutes": age_minutes(latest_active)}
        ),
        "checkpoint_run": checkpoint_run,
        "checkpoint_covered": checkpoint_covered,
        "last_24_hours": {
            "total": len(recent),
            "successful": successful_recent,
            "failed": failed_recent,
            "cancelled": cancelled_recent,
            "minimum_healthy": minimum_runs_24h,
            "nominal_schedule": NOMINAL_RUNS_24H,
        },
    }


def _api_request(
    url: str,
    *,
    token: str,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
) -> Any:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "wickless-scanner-health/2",
        },
        method=method,
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read()
        return None if not body else json.loads(body)


def fetch_runs(repository: str, token: str, workflow: str) -> list[dict[str, Any]]:
    result = _api_request(
        f"https://api.github.com/repos/{repository}/actions/workflows/{workflow}/runs?per_page=100",
        token=token,
    )
    return list(result["workflow_runs"])


def dispatch_workflow(repository: str, token: str, workflow: str, ref: str) -> None:
    _api_request(
        f"https://api.github.com/repos/{repository}/actions/workflows/{workflow}/dispatches",
        token=token,
        method="POST",
        payload={"ref": ref},
    )


def wait_for_recovery(
    repository: str,
    token: str,
    workflow: str,
    *,
    before_ids: set[int],
    existing_active_id: int | None,
    timeout_seconds: int = 300,
) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        runs = _sorted_runs(fetch_runs(repository, token, workflow))
        candidate = None
        if existing_active_id is not None:
            candidate = next(
                (row for row in runs if int(row["id"]) == existing_active_id), None
            )
        if candidate is None:
            candidate = next(
                (
                    row
                    for row in runs
                    if int(row["id"]) not in before_ids
                    and row.get("event") == "workflow_dispatch"
                ),
                None,
            )
        if candidate and candidate.get("status") == "completed":
            return candidate
        time.sleep(10)
    return None


def post_discord(webhook: str, message: str) -> None:
    body = json.dumps(
        {"content": message, "allowed_mentions": {"parse": []}}
    ).encode("utf-8")
    for attempt in range(1, 4):
        request = urllib.request.Request(
            webhook,
            data=body,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "wickless-scanner-health/2",
            },
            method="POST",
        )
        try:
            urllib.request.urlopen(request, timeout=20).read()
            return
        except (urllib.error.HTTPError, urllib.error.URLError):
            if attempt == 3:
                raise
            time.sleep(2 ** (attempt - 1))


def render_message(report: dict[str, Any]) -> str:
    icon = {"HEALTHY": "🫀", "DEGRADED": "⚠️", "UNHEALTHY": "❌"}[report["state"]]
    latest = report.get("latest_run")
    latest_text = "no run found"
    latest_url = None
    if latest:
        latest_text = (
            f"{latest.get('conclusion') or latest.get('status')} "
            f"{latest['age_minutes']:.1f} minutes ago"
        )
        latest_url = latest.get("html_url")
    checkpoint_run = report.get("checkpoint_run")
    checkpoint_text = "missed"
    if checkpoint_run:
        delta = (
            parse_timestamp(checkpoint_run["created_at"])
            - parse_timestamp(report["checkpoint_utc"])
        ).total_seconds() / 60
        checkpoint_text = f"success {delta:+.1f} minutes from checkpoint"
    recent = report["last_24_hours"]
    lines = [
        f"{icon} **Wickless scanner heartbeat — {report['state']}**",
        f"UTC: `{report['generated_at_utc']}`",
        f"London: `{report['generated_at_london']}`",
        (
            f"Checkpoint: `{report['checkpoint_london']}` | "
            f"heartbeat delay {report['heartbeat_delay_minutes']:.1f} minutes"
        ),
        f"Latest scan: {latest_text}",
    ]
    if latest_url:
        lines.append(f"Latest run: {latest_url}")
    lines.extend(
        (
            f"Checkpoint coverage: {checkpoint_text}",
            (
                "Last 24h: "
                f"{recent['total']} runs | {recent['successful']} success | "
                f"{recent['failed']} failed | {recent['cancelled']} cancelled "
                f"(healthy minimum {recent['minimum_healthy']}; nominal {recent['nominal_schedule']})"
            ),
        )
    )
    recovery = report.get("recovery")
    if recovery:
        recovery_line = f"Recovery: {recovery['status']}"
        if recovery.get("url"):
            recovery_line += f" | {recovery['url']}"
        lines.append(recovery_line)
    if report["reasons"]:
        lines.append("Reason: " + "; ".join(report["reasons"][:4]))
    lines.append("No trade signal is implied by this health message.")
    message = "\n".join(lines)
    if len(message) > 2000:
        raise RuntimeError("Scanner health message exceeds Discord's 2,000-character limit")
    return message


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--workflow", default="live-signals.yml")
    parser.add_argument("--ref", default="main")
    parser.add_argument("--event-name", default="workflow_dispatch")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--recovery-timeout-seconds", type=int, default=300)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    token = os.environ.get("GH_TOKEN")
    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    if not token:
        raise SystemExit("GH_TOKEN is not configured")
    if not webhook:
        raise SystemExit("DISCORD_WEBHOOK_URL is not configured")

    now = datetime.now(UTC)
    checkpoint = latest_scheduled_checkpoint(now, event_name=args.event_name)
    runs = fetch_runs(args.repository, token, args.workflow)
    report = evaluate_runs(runs, now=now, checkpoint=checkpoint)

    if report["needs_recovery"]:
        before_ids = {int(row["id"]) for row in runs}
        active = report.get("latest_active")
        active_id = int(active["id"]) if active else None
        if active_id is None:
            dispatch_workflow(args.repository, token, args.workflow, args.ref)
            recovery_mode = "dispatched"
        else:
            recovery_mode = "waited for active scan"
        recovery = wait_for_recovery(
            args.repository,
            token,
            args.workflow,
            before_ids=before_ids,
            existing_active_id=active_id,
            timeout_seconds=args.recovery_timeout_seconds,
        )
        refreshed_now = datetime.now(UTC)
        refreshed_runs = fetch_runs(args.repository, token, args.workflow)
        report = evaluate_runs(
            refreshed_runs,
            now=refreshed_now,
            checkpoint=checkpoint,
        )
        if recovery and recovery.get("conclusion") == "success":
            report["recovery"] = {
                "status": f"{recovery_mode} and succeeded",
                "run_id": recovery["id"],
                "url": recovery.get("html_url"),
            }
            if report["state"] == "UNHEALTHY":
                report["state"] = "DEGRADED"
                report["healthy"] = False
        else:
            report["state"] = "UNHEALTHY"
            report["healthy"] = False
            report["recovery"] = {
                "status": f"{recovery_mode} but did not complete successfully",
                "run_id": None if recovery is None else recovery.get("id"),
                "url": None if recovery is None else recovery.get("html_url"),
            }
            report["reasons"].insert(0, "automatic recovery did not succeed")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    message = render_message(report)
    print(message)
    post_discord(webhook, message)
    return 1 if report["state"] == "UNHEALTHY" else 0


if __name__ == "__main__":
    raise SystemExit(main())
