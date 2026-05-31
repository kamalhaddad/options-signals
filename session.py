"""
Trading-session clock (America/New_York) for the live bot.

Encodes the tuned strategy's session rules so the live bot trades the same window the
backtest did: trade the open, NO new entries after ENTRY_CUTOFF (12:00 ET), flatten near
the close. All checks are timezone-aware so the bot is correct in any cloud region.
"""
from __future__ import annotations
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import strategy_core as sc

ET = ZoneInfo("America/New_York")
OPEN = dtime(9, 30)
CLOSE = dtime(16, 0)


def now_et() -> datetime:
    return datetime.now(ET)


def _mins(t: datetime) -> int:
    return t.hour * 60 + t.minute


def is_market_open(t: datetime | None = None) -> bool:
    t = t or now_et()
    return t.weekday() < 5 and OPEN <= t.time() <= CLOSE


def minutes_to_close(t: datetime | None = None) -> int:
    return 16 * 60 - _mins(t or now_et())


def in_entry_window(t: datetime | None = None) -> bool:
    """New entries allowed: market open, past skip-open, before ENTRY_CUTOFF, before skip-close."""
    t = t or now_et()
    if not is_market_open(t):
        return False
    mso = _mins(t) - _mins(datetime.combine(t.date(), OPEN, tzinfo=ET))
    if mso < sc.SKIP_OPEN_MINUTES:
        return False
    mtc = minutes_to_close(t)
    if 0 < mtc <= sc.SKIP_CLOSE_MINUTES:
        return False
    cutoff = getattr(sc, "ENTRY_CUTOFF_MINUTE", None)
    if cutoff is not None and _mins(t) > cutoff:
        return False
    return True


def is_eod(t: datetime | None = None) -> bool:
    """Within the skip-close window — flatten any open positions."""
    t = t or now_et()
    return is_market_open(t) and 0 < minutes_to_close(t) <= sc.SKIP_CLOSE_MINUTES
