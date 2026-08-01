#!/usr/bin/env python3
"""Run the wickless scanner continuously, aligned to finalized 15m candles."""

from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence

from live_data import refresh
from wickless_bot import (
    DEFAULT_MAX_SIGNAL_AGE_SECONDS,
    LIVE_INSTRUMENTS,
    TIMEFRAME_MINUTES,
    scan_markets,
)


UTC = timezone.utc
STOP = False


def _request_stop(_signum: int, _frame: object) -> None:
    global STOP
    STOP = True


def seconds_until_next_scan(now: datetime, grace_seconds: int = 20) -> float:
    """Return seconds until the next candle close plus a feed grace period."""

    now = now.astimezone(UTC)
    floored = now.replace(second=0, microsecond=0)
    next_minute = (
        (floored.minute // TIMEFRAME_MINUTES) + 1
    ) * TIMEFRAME_MINUTES
    if next_minute >= 60:
        boundary = floored.replace(minute=0) + timedelta(hours=1)
    else:
        boundary = floored.replace(minute=next_minute)
    target = boundary + timedelta(seconds=grace_seconds)
    return max(1.0, (target - now).total_seconds())


def run_once(
    *,
    data_dir: Path,
    state_path: Path,
    instruments: Sequence[str],
    dry_run: bool,
    as_of: datetime | None = None,
) -> tuple[int, int]:
    now = (as_of or datetime.now(UTC)).astimezone(UTC)
    refresh(data_dir, instruments=instruments, now=now)
    return scan_markets(
        data_dir=data_dir,
        instruments=instruments,
        state_path=state_path,
        as_of=now,
        max_signal_age_seconds=DEFAULT_MAX_SIGNAL_AGE_SECONDS,
        state_retention_days=14,
        webhook_url=os.getenv("DISCORD_WEBHOOK_URL"),
        dry_run=dry_run,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path(".runtime-data"))
    parser.add_argument("--state", type=Path, default=Path(".signal-state/seen.json"))
    parser.add_argument(
        "--instruments",
        nargs="+",
        default=list(LIVE_INSTRUMENTS),
        choices=LIVE_INSTRUMENTS,
    )
    parser.add_argument("--grace-seconds", type=int, default=20)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run immediately once; useful for deployment health checks",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.dry_run and not os.getenv("DISCORD_WEBHOOK_URL"):
        print("error: DISCORD_WEBHOOK_URL is missing", file=sys.stderr)
        return 2

    for event in (signal.SIGINT, signal.SIGTERM):
        signal.signal(event, _request_stop)

    while not STOP:
        if not args.once:
            delay = seconds_until_next_scan(
                datetime.now(UTC),
                grace_seconds=args.grace_seconds,
            )
            print(f"Next scan in {delay:.1f}s", flush=True)
            deadline = time.monotonic() + delay
            while not STOP and time.monotonic() < deadline:
                time.sleep(min(1.0, deadline - time.monotonic()))
        if STOP:
            break
        try:
            found, posted = run_once(
                data_dir=args.data_dir,
                state_path=args.state,
                instruments=args.instruments,
                dry_run=args.dry_run,
            )
            print(f"Scan complete: {found} fresh, {posted} newly handled", flush=True)
        except (OSError, ValueError, RuntimeError) as error:
            print(f"Scan failed: {error}", file=sys.stderr, flush=True)
            if args.once:
                return 1
        if args.once:
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
