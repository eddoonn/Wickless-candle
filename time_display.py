"""Shared UTC and Europe/London timestamp rendering helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo


UTC = timezone.utc
LONDON = ZoneInfo("Europe/London")


def parse_utc_timestamp(value: str) -> datetime:
    """Parse an ISO timestamp and normalize it to UTC."""

    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"Timestamp must include a timezone: {value}")
    return parsed.astimezone(UTC)


def london_iso(value: str) -> str:
    """Return the same instant in Europe/London, including its UTC offset."""

    return parse_utc_timestamp(value).astimezone(LONDON).isoformat(timespec="seconds")


def timestamp_label(value: str, *, zone: ZoneInfo | timezone) -> str:
    """Render an ISO timestamp with a short timezone name."""

    converted = parse_utc_timestamp(value).astimezone(zone)
    name = converted.tzname() or str(zone)
    return f"{converted.isoformat(timespec='seconds')} ({name})"


def utc_london_text(value: str) -> str:
    """Render one instant as paired UTC and London lines for Discord or logs."""

    if not value:
        return "`not available`"
    return "\n".join(
        (
            f"`UTC {timestamp_label(value, zone=UTC)}`",
            f"`London {timestamp_label(value, zone=LONDON)}`",
        )
    )
