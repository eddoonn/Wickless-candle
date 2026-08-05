"""DST-aware production session union shared by live and research entrypoints."""

from __future__ import annotations

from dataclasses import replace
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
    """Return whether a bar opens in fixed London or New York production hours.

    Each side of the union is converted with its own IANA timezone, so UK and
    US daylight-saving transitions remain independent. ``use_session=False``
    retains the existing all-hours diagnostic behavior while still containing
    both production sessions. Candidate clock fields cannot narrow either
    production window because this predicate uses fixed constants.
    """

    if not config.use_session:
        return True
    london_open = bar.timestamp.astimezone(LONDON).time().replace(tzinfo=None)
    new_york_open = bar.timestamp.astimezone(NEW_YORK).time().replace(tzinfo=None)
    in_london = LONDON_START <= london_open < LONDON_END
    in_new_york = NEW_YORK_START <= new_york_open < NEW_YORK_END
    return in_london or in_new_york


def _install_live_signal_label() -> None:
    """Ensure live Discord records describe the session actually evaluated."""

    import wickless_bot

    if getattr(wickless_bot, "_production_session_label_installed", False):
        return
    original = wickless_bot.find_fresh_origin_limit_signals

    def with_production_session_label(*args: Any, **kwargs: Any) -> list[Any]:
        return [
            replace(signal, session_label=SESSION_LABEL)
            for signal in original(*args, **kwargs)
        ]

    wickless_bot.find_fresh_origin_limit_signals = with_production_session_label
    wickless_bot._production_session_label_installed = True


def install_production_session() -> None:
    """Install the union into shared live and research behavior for this process."""

    import no_wick_research

    no_wick_research._in_entry_session = in_production_session
    _install_live_signal_label()
