#!/usr/bin/env python3
"""Records a closed trade to data/trade_history.json.

positions.json only ever tracks OPEN positions -- once a position closes
(broker stop-out found during reconciliation, an ordinary/overbought-hold
signal exit, or an earnings-forced exit) it's simply removed from there. This
script is the durable record of what actually happened: the hourly-signal-check
skill calls it once for every exit path, right after (or, for a stop-out,
instead of) placing/finding the closing order, and before clearing the
position from positions.json.

Input (JSON, via --input file or stdin):
  {
    "symbol": "AAPL",
    "position": {"entry_price": 200.0, "qty": 0.5, "entry_time": "...",
                 "entry_order_id": "...", "stop_price": 195.0, "stop_order_id": "..."},
    "exit_price": 205.0,          # null if the fill price couldn't be confirmed
    "exit_time": "2026-07-30T15:00:00Z",
    "exit_order_id": "order-3",   # the stop order id for a stop_out, the new sell order id otherwise
    "exit_reason": "stop_out" | "signal_exit" | "earnings_exit"
  }

Output (JSON, to stdout): the full trade-history record as written.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.logging_utils import EventLogger
from lib.state import StateStore, close_trade_record


def _load_input(input_arg: str) -> dict:
    if input_arg == "-":
        return json.load(sys.stdin)
    with open(input_arg) as f:
        return json.load(f)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default="-", help="JSON input file, or '-' for stdin (default)")
    parser.add_argument("--data-dir", default="data")
    args = parser.parse_args()

    payload = _load_input(args.input)
    store = StateStore(args.data_dir)
    logger = EventLogger(args.data_dir)

    closed_at = datetime.now(timezone.utc).isoformat()
    record = close_trade_record(
        symbol=payload["symbol"],
        position=payload["position"],
        exit_price=payload.get("exit_price"),
        exit_time=payload["exit_time"],
        exit_order_id=payload.get("exit_order_id"),
        exit_reason=payload["exit_reason"],
        closed_at=closed_at,
    )

    store.append_trade_history(record)
    logger.log("trade_closed", **record)

    json.dump(record, sys.stdout)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
