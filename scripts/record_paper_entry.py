#!/usr/bin/env python3
"""Writes a new paper (dry-run, no real order) position directly, given an
already-known entry price.

Superseded as of 2026-08-14 for the daily-stochastic-check skill's normal
flow: a confirmed signal is now queued (scripts/queue_paper_fill.py) and
settled at the next trading day's open (scripts/settle_paper_fills.py)
instead of being written immediately at the signal day's close, since that
close-price fill was never actually achievable (this track's routine only
runs once per day, after close). Kept as a standalone, tested utility for
direct/manual position writes (e.g. manual reconciliation) where the entry
price is already known and doesn't need next-open settlement logic.

Input (JSON, via --input file or stdin):
  {
    "symbol": "AAPL", "entry_price": 209.9, "qty": 1, "entry_time": "2026-08-13T00:00:00Z",
    "stop_price": 205.2, "entry_k": 24.1, "entry_d": 19.8, "entry_prev_k": 18.6, "entry_prev_d": 15.2
  }

Output (JSON, to stdout): the position record as written.

No slot-availability check here (mirrors the rest of this codebase's split:
control flow like "how many slots are open" belongs to the calling skill,
not to a script that just performs one write) -- the daily-stochastic-check
skill is responsible for only calling this up to however many slots section
2 found available.
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


def record_paper_entry(payload: dict, store: StateStore) -> dict:
    position = {
        "entry_price": payload["entry_price"],
        "qty": payload["qty"],
        "entry_time": payload["entry_time"],
        "entry_order_id": "paper",
        "stop_order_id": None,
        "stop_price": payload["stop_price"],
        "stochastic_state": "NORMAL",
        "entry_k": payload.get("entry_k"),
        "entry_d": payload.get("entry_d"),
        "entry_prev_k": payload.get("entry_prev_k"),
        "entry_prev_d": payload.get("entry_prev_d"),
    }
    positions = store.load_positions()
    positions[payload["symbol"]] = position
    store.save_positions(positions)
    return position


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", default="-", help="JSON input file, or '-' for stdin (default)")
    parser.add_argument("--data-dir", default="data")
    args = parser.parse_args()

    payload = _load_input(args.input)
    store = StateStore(args.data_dir)
    logger = EventLogger(args.data_dir)

    position = record_paper_entry(payload, store)
    logger.log("paper_entry_recorded", symbol=payload["symbol"], **position)

    json.dump({"symbol": payload["symbol"], **position}, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
