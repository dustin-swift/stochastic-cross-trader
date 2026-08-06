#!/usr/bin/env python3
"""Filters already-confirmed entry signals against fresh earnings data,
right before buying (2026-08-06, at the user's direction).

Previously the hourly-signal-check skill fetched earnings for every
candidate up front, every cycle — hundreds of single-symbol
get_earnings_results calls (that endpoint has no batch mode), almost all
wasted since only a handful of candidates ever actually confirm a signal in
a given cycle. That was the dominant cost in the whole cycle's latency, far
more than the batched hourly-bars fetch.

This script moves the earnings check to AFTER the stochastic dual-cross
confirmation instead of before it: check_hourly_signals.py's `entries[]`
output (already a short list, typically 0-5 symbols) is the only thing
that needs a fresh earnings lookup — so we don't buy something and then
have to immediately sell it because it turns out to report tomorrow. Open
positions' earnings are handled separately and much less frequently (see
the hourly-signal-check skill's exit section) — this script is entries-only.

Input (JSON, via --input file or stdin):
  {
    "entries": [{"symbol": "AAPL", ...}, ...],  # check_hourly_signals.py's entries[] output, verbatim
    "earnings_by_symbol": {"AAPL": [{"date": "2026-08-15", "timing": "am"}], ...},
    "today": "2026-08-06"  # optional, ISO date; defaults to the real current date
  }

`earnings_by_symbol` should only include a symbol after a successful
get_earnings_results fetch — a symbol omitted entirely (fetch failed, or
simply never attempted) is treated as "not successfully checked" and
excluded conservatively (lib.catalysts.filter_earnings_entry_blocked's
"unknown" reason). This is a deliberate change from the old defense-in-depth
design, where an unchecked candidate fell through to "not re-checked here,
the daily screen is the primary gate" — this script IS now the primary gate
for entries, so failing closed on an unconfirmed fetch is the safer default.

Output (JSON, to stdout): the filtered entries list (earnings-blocked ones
removed). Logs `entries_earnings_excluded` (symbols + reasons) when
anything gets dropped; logs nothing when `entries` is already empty or
nothing gets excluded.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.catalysts import filter_earnings_entry_blocked
from lib.logging_utils import EventLogger


def _load_input(input_arg: str) -> dict:
    if input_arg == "-":
        return json.load(sys.stdin)
    with open(input_arg) as f:
        return json.load(f)


def run(payload: dict, logger: EventLogger, today: date) -> list[dict]:
    entries = payload.get("entries", [])
    earnings_by_symbol = payload.get("earnings_by_symbol", {})

    kept, excluded = filter_earnings_entry_blocked(entries, earnings_by_symbol, as_of=today)

    if excluded:
        logger.log(
            "entries_earnings_excluded",
            symbols=[c["symbol"] for c in excluded],
            reasons={c["symbol"]: c["earnings_exclusion_reason"] for c in excluded},
        )

    return kept


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default="-", help="JSON input file, or '-' for stdin (default)")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument(
        "--today",
        default=None,
        help="Override today's date (ISO YYYY-MM-DD) -- for tests. Defaults to the real current date.",
    )
    args = parser.parse_args()

    payload = _load_input(args.input)
    logger = EventLogger(args.data_dir)
    today = date.fromisoformat(args.today) if args.today else date.today()

    kept = run(payload, logger, today)

    json.dump(kept, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
