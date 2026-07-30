#!/usr/bin/env python3
"""Records and alerts on a stop-order placement failure after retry (spec
§6b, Order Lifecycle step 6). Called by the hourly-signal-check skill exactly
once, after an entry has filled but the immediate follow-up resting stop_market
order fails to place even after one retry — a live, unprotected position is
the one failure mode this whole design exists to avoid, so this always alerts,
unconditionally (no branching decision logic, unlike evaluate_order_fill.py).

Input (JSON, via --input file or stdin):
  {"symbol": "AAPL", "qty": 0.5, "fill_price": 210.5, "error": "..."}

Output (JSON, to stdout): the same payload, echoed back for confirmation.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.alerts import send_alert
from lib.logging_utils import EventLogger


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
    logger = EventLogger(args.data_dir)

    symbol = payload["symbol"]
    qty = payload["qty"]
    fill_price = payload["fill_price"]
    error = payload.get("error", "unknown error")

    message = (
        f"CRITICAL: stop-market order failed to place for {symbol} after retry "
        f"(qty={qty}, fill_price={fill_price}). Position is UNPROTECTED. Error: {error}"
    )

    logger.log("stop_placement_failed", symbol=symbol, qty=qty, fill_price=fill_price, error=error)
    send_alert("stop_placement_failed", message)

    json.dump(payload, sys.stdout)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
