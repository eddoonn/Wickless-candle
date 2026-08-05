"""DST-aware production session union shared by live and research entrypoints."""

from __future__ import annotations

from datetime import time
from typing import Any
from zoneinfo import ZoneInfo


LONDON = ZoneInfo("Europe/London")
NEW_YORK = ZoneInfo("America/New_York")
LONDON_START = time(8, 0)
LONDON_END = time(17, 0)
NEW_YORK_START = time(5, 0)
NEW_YORK_END = time(13, 30)
SESSION_LABEL = (
    "08:00-17:00 Europe/London OR 05:00-13:30 America/New_York"
)


def in_production_session(bar: Any, config: Any) -> bool:
    """Return whether a bar opens in London or New York production hours.

    Each side of the union is converted with its own IANA timezone, so UK and
    US daylight-saving transitions remain independent. ``use_session=False``
    retains the existing all-hours diagnostic behavior while still containing
    both production sessions.
    """

    if not config.use_session:
        return True
    london_open = bar.timestamp.astimezone(LONDON).time().replace(tzinfo=None)
    new_york_open = bar.timestamp.astimezone(NEW_YORK).time().replace(tzinfo=None)
    in_london = LONDON_START <= london_open < LONDON_END
    in_new_york = config.session_start <= new_york_open < config.session_end
    return in_london or in_new_york


def install_production_session() -> None:
    """Install the union into the shared no-wick engine for this process."""

    import no_wick_research

    no_wick_research._in_entry_session = in_production_session
