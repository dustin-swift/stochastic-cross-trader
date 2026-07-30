"""Earnings/catalyst avoidance. Pure functions, no network calls — callers
fetch report dates via Robinhood MCP (get_earnings_results, one call per
symbol) and pass them in.

Motivation: the resting protective stop (`stop_market`) can only be placed
regular-hours — it's a hard platform constraint, not a config choice (see
tool inventory). A stock that gaps hard on an earnings reaction, pre-market or
after-hours, has zero stop protection until regular hours resume, and the
eventual fill can be well below the intended stop. Avoiding known earnings
dates — both when opening new positions and while already holding one — is
the mitigation, since there's no way to protect against the gap itself.
"""
from __future__ import annotations

from datetime import date, datetime


def _parse_date(value: str) -> date:
    # Accept a plain "YYYY-MM-DD" or a full ISO timestamp; either way we only
    # care about the calendar date for a "how many days until earnings" check.
    return datetime.fromisoformat(value.replace("Z", "+00:00")).date() if "T" in value else date.fromisoformat(value)


def days_until_next_earnings(report_dates: list[str], as_of: date) -> int | None:
    """Days from `as_of` to the nearest report date on or after `as_of`, from
    a list of date strings (as returned by get_earnings_results — mix of past
    and future reports). Returns None if none of the given dates are on or
    after `as_of` (e.g. all historical, or the list is empty) — i.e. "no known
    upcoming earnings in this data," not "definitely none ever."
    """
    upcoming = [d for d in (_parse_date(r) for r in report_dates) if d >= as_of]
    if not upcoming:
        return None
    return (min(upcoming) - as_of).days


def is_too_close_to_earnings(report_dates: list[str], as_of: date, max_days: int) -> bool:
    """True iff the nearest known upcoming earnings report is within
    `max_days` of `as_of` (inclusive — max_days=0 means "today only").
    """
    days = days_until_next_earnings(report_dates, as_of)
    return days is not None and days <= max_days


def filter_earnings_too_close(
    candidates: list[dict],
    earnings_by_symbol: dict[str, list[str]],
    as_of: date,
    max_days: int,
) -> tuple[list[dict], list[dict]]:
    """Split `candidates` (each a dict with a "symbol" key) into (kept,
    excluded) based on earnings proximity.

    A symbol missing from `earnings_by_symbol` entirely is treated as "not
    successfully checked" and excluded conservatively — distinct from a
    symbol present with an empty list, which means "checked, no upcoming
    report found" and is kept. Callers should only include a symbol in
    `earnings_by_symbol` after a successful fetch, so this distinction is
    meaningful rather than accidental.

    Each excluded candidate is annotated with `earnings_exclusion_reason`:
    "too_close" (a known report within max_days) or "unknown" (not checked).
    """
    kept, excluded = [], []
    for c in candidates:
        symbol = c["symbol"]
        if symbol not in earnings_by_symbol:
            excluded.append({**c, "earnings_exclusion_reason": "unknown"})
            continue
        if is_too_close_to_earnings(earnings_by_symbol[symbol], as_of, max_days):
            excluded.append({**c, "earnings_exclusion_reason": "too_close"})
            continue
        kept.append(c)
    return kept, excluded
