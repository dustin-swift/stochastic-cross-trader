"""Breakout/retest tracking state machine for the MA Pullback / Breakout-
Retest agent (spec §2-3). Pure, pandas-based, no network calls -- mirrors the
reference implementation's `daily_scan.py` but corrected and idiomatic to
this repo's `lib/indicators.py` conventions.

Corrections this module encodes, all confirmed by the reference spec as
having been real bugs during development (see the plan / README's "MA
Pullback" section for the full writeup):

- **`breakout_level` is the PRIOR `lookback`-bar high, never the breakout
  bar's own (possibly higher) high** — `detect_breakout` uses
  `lib.indicators.rolling_high(..., include_today=False)`. Fixing this was
  reportedly the single biggest driver of edge in the reference backtest.
- **Retest requires separation**: a pullback only counts as a genuine retest
  once the close has extended at least `min_extension_pct` above
  `breakout_level` AND at least `min_separation_days` trading days have
  passed since the breakout — a same-candle whipsaw back through the level
  is not a retest.
- **Failed breakout is sticky**: once `close < breakout_level * (1 -
  failed_breakout_depth)`, the entry stays `failed` on every subsequent call
  until a fresh breakout overwrites it entirely — it does not un-fail on its
  own.
- **Age-out**: a breakout more than `max_breakout_age_days` trading days old
  is dropped from the watchlist entirely (returns `None`), not just marked
  ineligible — a stale setup shouldn't keep occupying watchlist state.

`update_watchlist_entry` locates the breakout bar within the CURRENT call's
`bars` by matching `entry["breakout_date"]` against `bars["date"]`, rather
than trusting a previously-stored positional index — the daily scan re-fetches
price history independently each day (see `scripts/check_ma_daily_scan.py`),
so a bar array's absolute indexing isn't guaranteed to stay aligned with a
prior day's array. Date-matching is robust to that regardless of exactly how
far back each day's fetch reaches, as long as `breakout_date` is still inside
the window (which `max_breakout_age_days` + a reasonable lookback ensures).
"""
from __future__ import annotations

import pandas as pd

from lib.indicators import rolling_high


def detect_breakout(bars: pd.DataFrame, lookback: int = 252) -> float | None:
    """Fresh-breakout check on the LAST bar in `bars` (oldest-first, ending
    with the most recent completed daily bar): today's high vs. the PRIOR
    `lookback`-bar high (never today's own high -- see module docstring).

    Returns the prior high itself as `breakout_level` when today's high meets
    or exceeds it (a fresh breakout), else `None`. Requires strictly more
    than `lookback` bars of history (mirrors the reference implementation's
    `i > lookback` guard) -- otherwise the "prior" window doesn't actually
    have `lookback` full bars behind it yet.
    """
    if len(bars) <= lookback:
        return None

    prior_high = rolling_high(bars, lookback, column="high", include_today=False)
    prior_max_high = prior_high.iloc[-1]
    if pd.isna(prior_max_high):
        return None

    today_high = float(bars["high"].iloc[-1])
    if today_high >= prior_max_high:
        return float(prior_max_high)
    return None


def _bar_index_for_date(bars: pd.DataFrame, target_date: str) -> int | None:
    matches = bars.index[bars["date"].astype(str) == str(target_date)]
    if len(matches) == 0:
        return None
    # Use positional (not label) index of the last match, in case the date
    # column has duplicates -- shouldn't happen for daily bars, but positional
    # arithmetic below needs a plain integer either way.
    return bars.index.get_loc(matches[-1])


def update_watchlist_entry(entry: dict | None, bars: pd.DataFrame, config: dict) -> tuple[dict | None, bool]:
    """Update a single symbol's watchlist entry given its latest daily bars.

    `entry` is the symbol's current watchlist entry (or `None` if not yet
    tracked). `bars` must have columns date/high/low/close/volume,
    oldest-first, ending with the most recent completed daily bar. `config`
    is the loaded `config/ma_pullback_strategy.yaml` dict (`config["breakout"]`
    is what's read here).

    Returns `(entry_or_None, eligible_for_entry)`:
    - `entry_or_None`: the updated entry to persist (drop the symbol from the
      watchlist entirely if this is `None` -- stale/aged-out).
    - `eligible_for_entry`: whether `scripts/check_ma_hourly_signals.py`
      should evaluate this symbol for entry this cycle (`retest_seen`, not
      `failed`) -- also stored on the entry itself as
      `entry["eligible_for_entry"]`, so a plain `watchlist.json` read already
      carries it without recomputing.
    """
    breakout_cfg = config["breakout"]
    lookback = breakout_cfg["lookback_days"]
    max_age = breakout_cfg["max_breakout_age_days"]
    failed_depth = breakout_cfg["failed_breakout_depth"]
    min_separation = breakout_cfg["min_separation_days"]
    min_extension = breakout_cfg["min_extension_pct"]

    bars = bars.reset_index(drop=True)
    n = len(bars)
    # Same margin the reference implementation used before evaluating
    # anything -- not enough history yet to safely compute the prior-lookback
    # high (or a fresh breakout below the required strict-greater-than
    # threshold in detect_breakout).
    if n < lookback + 5:
        return entry, False

    i = n - 1
    today_high = float(bars["high"].iloc[i])
    today_low = float(bars["low"].iloc[i])
    today_close = float(bars["close"].iloc[i])
    today_date = str(bars["date"].iloc[i])
    today_volume = float(bars["volume"].iloc[i]) if "volume" in bars.columns else None

    breakout_level = detect_breakout(bars, lookback=lookback)
    is_fresh_breakout = breakout_level is not None

    new_entry = dict(entry) if entry else None

    if is_fresh_breakout:
        new_entry = {
            "breakout_date": today_date,
            "breakout_level": breakout_level,
            "breakout_volume": today_volume,
            "retest_seen": False,
            "retest_low": None,
            "failed": False,
            "max_close_since_breakout": today_close,
            "extension_confirmed": False,
        }

    if new_entry is None:
        return None, False

    if new_entry.get("failed"):
        # Sticky until a fresh breakout overwrites it (handled above, before
        # this check) -- a failed breakout never un-fails on its own.
        new_entry["eligible_for_entry"] = False
        return new_entry, False

    bi = _bar_index_for_date(bars, new_entry["breakout_date"])
    if bi is None:
        # breakout_date has rolled off the window this call was given -- with
        # max_breakout_age_days bounding how old a tracked breakout can be,
        # this should only happen if the caller's fetch window shrank
        # unexpectedly. Fail safe by dropping the entry rather than guessing
        # its age.
        return None, False

    days_since_breakout = i - bi
    if days_since_breakout > max_age:
        return None, False

    if days_since_breakout < 1:
        # Breakout day itself -- nothing to update yet (no separation, no
        # retest possible same-day).
        new_entry["eligible_for_entry"] = False
        return new_entry, False

    breakout_level = new_entry["breakout_level"]

    # --- extension confirmation (separation requirement) ---
    new_entry["max_close_since_breakout"] = max(new_entry["max_close_since_breakout"], today_close)
    if (
        new_entry["max_close_since_breakout"] >= breakout_level * (1 + min_extension)
        and days_since_breakout >= min_separation
    ):
        new_entry["extension_confirmed"] = True

    # --- retest tracking (only once extension is confirmed) ---
    if today_low <= breakout_level and new_entry["extension_confirmed"]:
        new_entry["retest_seen"] = True
        new_entry["retest_low"] = (
            today_low if new_entry["retest_low"] is None else min(new_entry["retest_low"], today_low)
        )

    # --- failed breakout (sticky) ---
    if today_close < breakout_level * (1 - failed_depth):
        new_entry["failed"] = True
        new_entry["eligible_for_entry"] = False
        return new_entry, False

    eligible = bool(new_entry["retest_seen"] and not new_entry["failed"])
    new_entry["eligible_for_entry"] = eligible
    return new_entry, eligible
