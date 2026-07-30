"""Reads a manually-exported Finviz Elite screener CSV from disk. No network
calls, no auth key — the user builds and exports the screen themselves in the
Finviz Elite web UI whenever they want to refine the filter criteria; this
module's only job is to read that file and make sure it isn't stale.
"""
from __future__ import annotations

import csv
import os
from datetime import date, datetime, timedelta
from pathlib import Path

REQUIRED_COLUMNS = ("Ticker", "Sector")


def load_universe(csv_path: str) -> list[dict]:
    """Parse the Finviz export at `csv_path` into a list of row dicts (one per
    symbol, keyed by the CSV's own header names). Raises FileNotFoundError if
    the path doesn't exist, ValueError if required columns are missing.
    """
    path = Path(csv_path)
    if not path.exists():
        raise FileNotFoundError(
            f"Finviz export not found at {csv_path} — export it from the Finviz "
            "Elite screener and save it there before running this."
        )

    with path.open(newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        missing = [c for c in REQUIRED_COLUMNS if c not in fieldnames]
        if missing:
            raise ValueError(
                f"Finviz export at {csv_path} is missing required column(s): {missing} "
                f"(found columns: {fieldnames})"
            )
        return list(reader)


def _previous_trading_day(d: date) -> date:
    """The most recent trading day strictly before `d`, treating only
    Saturday/Sunday as non-trading days.

    Known limitation (v1): market holidays aren't accounted for, only
    weekends — e.g. the trading day after a Monday holiday will incorrectly
    treat the preceding Friday's export as still-fresh on that Tuesday.
    Acceptable gap for now; flagged here and in the README rather than left
    silent.
    """
    prev = d - timedelta(days=1)
    while prev.weekday() >= 5:  # Saturday=5, Sunday=6
        prev -= timedelta(days=1)
    return prev


def check_freshness(csv_path: str, today: date) -> None:
    """Raise if the Finviz export at `csv_path` is missing or stale.

    "Stale" means last-modified before the most recent trading day prior to
    `today` — e.g. a Friday export is still valid Saturday through Monday,
    but stale starting Tuesday. Callers must not proceed (must not write
    candidates.json) if this raises.
    """
    path = Path(csv_path)
    if not path.exists():
        raise FileNotFoundError(
            f"Finviz export not found at {csv_path} — export it from the Finviz "
            "Elite screener and save it there before running this."
        )

    mtime_date = datetime.fromtimestamp(os.path.getmtime(path)).date()
    required = _previous_trading_day(today)
    if mtime_date < required:
        raise ValueError(
            f"Finviz export at {csv_path} is stale: last modified {mtime_date}, "
            f"but needs to be on or after {required} (as of {today}). "
            "Re-run the Finviz Elite screener and re-export."
        )
