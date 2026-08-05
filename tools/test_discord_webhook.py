#!/usr/bin/env python3
"""Send one clearly labelled, non-trading Discord connectivity test."""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from time_display import LONDON, timestamp_label
from wickless_bot import validate_webhook_url


UTC = timezone.utc


def connectivity_message(now: datetime | None = None) -> str:
    """Return the labelled test message with paired UTC and London timestamps."""

    instant = (now or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
    value = instant.isoformat()
    return "\n".join(
        (
            "**Wickless 15m connectivity test**",
            "The webhook is reachable. No trade signal was generated or placed.",
            f"UTC: `{timestamp_label(value, zone=UTC)}`",
            f"London: `{timestamp_label(value, zone=LONDON)}`",
        )
    )


def main() -> int:
    raw_url = os.getenv("DISCORD_WEBHOOK_URL", "")
    if not raw_url:
        print("Set DISCORD_WEBHOOK_URL before running this test.", file=sys.stderr)
        return 2
    payload = {
        "username": "Wickless 15m Signals",
        "allowed_mentions": {"parse": []},
        "content": connectivity_message(),
    }
    request = urllib.request.Request(
        validate_webhook_url(raw_url),
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "wickless-candle-webhook-test/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            if response.status not in {200, 204}:
                print(f"Unexpected Discord HTTP {response.status}.", file=sys.stderr)
                return 1
    except urllib.error.HTTPError as error:
        print(f"Discord rejected the test with HTTP {error.code}.", file=sys.stderr)
        return 1
    except urllib.error.URLError as error:
        print(f"Could not reach Discord: {error.reason}", file=sys.stderr)
        return 1
    print("Discord connectivity test sent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
