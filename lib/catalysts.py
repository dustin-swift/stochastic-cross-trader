"""Earnings/catalyst avoidance. Pure functions, no network calls — callers
fetch report date + timing via Robinhood MCP (get_earnings_results, one call
per symbol) and pass them in.

Motivation: the resting protective stop (`stop_market`) can only be placed
regular-hours — it's a hard platform constraint, not a config choice (see
tool inventory). A stock that gaps hard on an earnings reaction, pre-market or
after-hours, has zero stop protection until regular hours resume, and the
eventual fill can be well below the intended stop. Avoiding known earnings
dates — both when opening new positions and while already holding one — is
the mitigation, since there's no way to protect against the gap itself.

Trigger-date rule (BMO/AMC-aware): a report's *exit date* — the last trading
day it's safe to be holding through the close of — depends on when it's
reported:

- **BMO** (before market open) reports on date D: the regular session on D
  itself already opens knowing the news, so the position must be closed by
  D-1's close. exit_date = D - 1.
- **AMC** (after market close) reports on date D: the regular session on D
  is unaffected by the report (it comes after that session ends), so the
  position can safely hold through all of D and must be closed by D's own
  close. exit_date = D.
- **Unknown/missing timing**: treated as BMO (exit_date = D - 1) — the more
  conservative choice, since the whole point of this rule is protecting
  against an unprotected gap and we'd rather close a day early on an unknown
  than a day late.

A held position is force-exited (regardless of stochastic state) on the
LAST regular-session check of its nearest upcoming exit_date (`near_close`
gating, see `is_forced_exit_day` — fixed 2026-08-04, previously fired on the
FIRST check of that day instead: confirmed live on BALL, exited ~9:45am ET
on its exit_date rather than holding through the full session as intended,
forfeiting most of that day's run-up). A day already past its exit_date
(the check was somehow missed at close, e.g. a routine failure) still force-
exits immediately regardless of time of day — better to close late than not
at all. A new entry is blocked only on the exit_date itself
(`is_entry_blocked_day`) — entering earlier still captures the run-up into
the report; entering exactly on the exit_date would get force-closed later
that same day, which is a near-zero-value trade not worth taking. There is
deliberately no wider entry-avoidance window beyond that single day.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

_CONSERVATIVE_TIMING = "am"  # unknown/missing timing treated as BMO


def _parse_date(value: str) -> date:
    # Accept a plain "YYYY-MM-DD" or a full ISO timestamp; either way we only
    # care about the calendar date for exit-date math.
    return datetime.fromisoformat(value.replace("Z", "+00:00")).date() if "T" in value else date.fromisoformat(value)


def _normalize_report(report: dict | str) -> tuple[date, str]:
    """Accepts either the current `{"date": "YYYY-MM-DD", "timing": "am"|"pm"}`
    shape (as sourced from get_earnings_results' report.date/report.timing),
    or a bare date string for backward compatibility with earnings data
    fetched/persisted before the BMO/AMC-aware rule existed — those have no
    timing info, so they get the conservative default.
    """
    if isinstance(report, str):
        return _parse_date(report), _CONSERVATIVE_TIMING
    timing = report.get("timing") or _CONSERVATIVE_TIMING
    return _parse_date(report["date"]), timing


def earnings_exit_date(report_date: date, timing: str) -> date:
    """The last trading day it's safe to hold through the close of, given a
    single report's date and timing ("am" = BMO, "pm" = AMC; anything else is
    treated as the conservative "am" default by the caller)."""
    return report_date if timing == "pm" else report_date - timedelta(days=1)


def next_forced_exit_date(reports: list[dict | str], as_of: date) -> date | None:
    """The nearest exit_date, among reports whose report date hasn't already
    passed as of `as_of`, that is due (i.e. the minimum exit_date — which may
    already be <= as_of, meaning it's due today or overdue). Returns None if
    there's no upcoming report in `reports`.
    """
    upcoming = [_normalize_report(r) for r in reports if _parse_date(r if isinstance(r, str) else r["date"]) >= as_of]
    if not upcoming:
        return None
    return min(earnings_exit_date(report_date, timing) for report_date, timing in upcoming)


def is_forced_exit_day(reports: list[dict | str], as_of: date, near_close: bool = True) -> bool:
    """True iff a held position should be force-exited on this check,
    regardless of stochastic state.

    `near_close` (2026-08-04): the caller's own signal for "this is the last
    regular-session check of the day", since this module has no wall-clock
    awareness of its own. When `as_of` is exactly the exit_date, the exit
    only fires if `near_close` is true — the whole point of the exit_date
    rule is to let the position hold through as much of that day's session
    as safely possible (D-1 for BMO, D itself for AMC), so exiting on the
    FIRST check of that day instead of the last one forfeits most of the
    intended run-up. `as_of` already past the exit_date (overdue — e.g. a
    prior day's close check was missed) always forces the exit regardless
    of `near_close`, since waiting further only adds gap risk. Defaults to
    `True` so existing callers that haven't been updated to pass real
    wall-clock timing keep the old (safe, if early) behavior rather than
    silently never firing.
    """
    exit_date = next_forced_exit_date(reports, as_of)
    if exit_date is None:
        return False
    if as_of > exit_date:
        return True
    return as_of == exit_date and near_close


def is_entry_blocked_day(reports: list[dict | str], as_of: date) -> bool:
    """True iff a new entry should be skipped today because it would be
    force-exited (see `is_forced_exit_day`) almost immediately — i.e. today
    is exactly the nearest upcoming report's exit_date. Days before this are
    intentionally not blocked, so entries still capture the run-up into the
    report."""
    exit_date = next_forced_exit_date(reports, as_of)
    return exit_date is not None and as_of == exit_date


def filter_earnings_entry_blocked(
    candidates: list[dict],
    earnings_by_symbol: dict[str, list[dict | str]],
    as_of: date,
) -> tuple[list[dict], list[dict]]:
    """Split `candidates` (each a dict with a "symbol" key) into (kept,
    excluded) based on the entry-blocked-day rule above.

    A symbol missing from `earnings_by_symbol` entirely is treated as "not
    successfully checked" and excluded conservatively — distinct from a
    symbol present with an empty list, which means "checked, no upcoming
    report found" and is kept. Callers should only include a symbol in
    `earnings_by_symbol` after a successful fetch, so this distinction is
    meaningful rather than accidental.

    Each excluded candidate is annotated with `earnings_exclusion_reason`:
    "too_close" (today is the report's exit_date) or "unknown" (not checked).
    """
    kept, excluded = [], []
    for c in candidates:
        symbol = c["symbol"]
        if symbol not in earnings_by_symbol:
            excluded.append({**c, "earnings_exclusion_reason": "unknown"})
            continue
        if is_entry_blocked_day(earnings_by_symbol[symbol], as_of):
            excluded.append({**c, "earnings_exclusion_reason": "too_close"})
            continue
        kept.append(c)
    return kept, excluded
