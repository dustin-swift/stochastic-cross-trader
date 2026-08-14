#!/usr/bin/env python3
"""Queues a confirmed daily-stochastic-check entry signal for a next-open
fill instead of buying it immediately (2026-08-14, at the user's direction).

Why: the daily track only runs once per day, after close. Filling a signal
confirmed off TODAY's close at TODAY's close price (the original design) is
a fill no real order could ever actually achieve -- by the time this cycle
even runs, today's regular session is already over. The earliest fill a
real (or realistically-simulated) system could get is TOMORROW's open. This
script just records that a signal fired and what its context was; the
actual position isn't written until scripts/settle_paper_fills.py runs on
the NEXT cycle armed with tomorrow's now-known open price.

Input (JSON, via --input file or stdin):
  {"symbol": "AAPL", "atr14": 3.2, "entry_k": 24.1, "entry_d": 19.8,
   "entry_prev_k": 18.6, "entry_prev_d": 15.2, "signal_date": "2026-08-14"}

Output (JSON, to stdout): the queued record, as written.

Logs `paper_fill_queued`. Overwrites any existing pending-fill entry for the
same symbol (shouldn't normally happen -- build_entries_payload.py already
excludes symbols with a pending fill from candidate evaluation -- but last
write wins rather than erroring, consistent with the rest of this codebase's
write scripts).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.logging_utils import EventLogger
from lib.state import StateStore


def _load_input(input_arg: str) -> dict:
    if input_arg == "-":
        return json.load(sys.stdin)
    with open(input_arg) as f:
        return json.load(f)


def queue_paper_fill(payload: dict, store: StateStore) -> dict:
    record = {
        "atr14": payload.get("atr14"),
        "entry_k": payload.get("entry_k"),
        "entry_d": payload.get("entry_d"),
        "entry_prev_k": payload.get("entry_prev_k"),
        "entry_prev_d": payload.get("entry_prev_d"),
        "signal_date": payload.get("signal_date"),
    }
    pending = store.load_pending_fills()
    pending[payload["symbol"]] = record
    store.save_pending_fills(pending)
    return record


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", default="-", help="JSON input file, or '-' for stdin (default)")
    parser.add_argument("--data-dir", default="data")
    args = parser.parse_args()

    payload = _load_input(args.input)
    store = StateStore(args.data_dir)
    logger = EventLogger(args.data_dir)

    record = queue_paper_fill(payload, store)
    logger.log("paper_fill_queued", symbol=payload["symbol"], **record)

    json.dump({"symbol": payload["symbol"], **record}, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
