#!/usr/bin/env python3
"""Run the live scanner with paired UTC and Europe/London Discord timestamps."""

from __future__ import annotations

from typing import Any, Sequence

import wickless_bot
from production_session import install_production_session
from time_display import utc_london_text


# The GitHub Actions live entrypoint always installs the same session union used
# by the reference baseline and candidate evaluator.
install_production_session()

_ORIGINAL_DISCORD_PAYLOAD = wickless_bot.discord_payload
_REPLACED_TIME_FIELDS = {
    "Published (UTC)",
    "Reclaim entry time (London)",
}


def dual_timezone_discord_payload(signal: Any) -> dict[str, object]:
    """Return the standard signal embed with all event times in both zones."""

    payload = _ORIGINAL_DISCORD_PAYLOAD(signal)
    embeds = payload.get("embeds")
    if not isinstance(embeds, list) or not embeds or not isinstance(embeds[0], dict):
        raise ValueError("Discord payload is missing its primary embed")
    embed = embeds[0]
    raw_fields = embed.get("fields")
    if not isinstance(raw_fields, list):
        raise ValueError("Discord payload embed is missing fields")
    fields = [
        field
        for field in raw_fields
        if isinstance(field, dict) and field.get("name") not in _REPLACED_TIME_FIELDS
    ]
    time_fields = [
        {
            "name": "Signal close time",
            "value": utc_london_text(signal.signal_time_utc),
            "inline": False,
        },
        {
            "name": "Entry time",
            "value": utc_london_text(signal.fill_time_utc),
            "inline": False,
        },
        {
            "name": "Detected time",
            "value": utc_london_text(signal.detected_time_utc),
            "inline": False,
        },
        {
            "name": "Published time",
            "value": utc_london_text(signal.published_time_utc),
            "inline": False,
        },
    ]
    signal_id_index = next(
        (
            index
            for index, field in enumerate(fields)
            if field.get("name") == "Signal ID"
        ),
        len(fields),
    )
    embed["fields"] = fields[:signal_id_index] + time_fields + fields[signal_id_index:]
    footer = embed.get("footer")
    if isinstance(footer, dict) and isinstance(footer.get("text"), str):
        footer["text"] += " • event times shown in UTC and Europe/London"
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    """Install the dual-time renderer and delegate to the existing scanner CLI."""

    wickless_bot.discord_payload = dual_timezone_discord_payload
    return wickless_bot.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
