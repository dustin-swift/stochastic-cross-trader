#!/usr/bin/env python3
"""Entry-order fill polling decision (spec §6b amendment). Thin CLI wrapping
lib.orders.evaluate_fill_status — the hourly-signal-check skill calls this
after each get_equity_orders poll during the entry lifecycle, and acts on
the returned decision.

Input (JSON, via --input file or stdin):
  {"symbol": "AAPL", "order_id": "...", "order_state": "partially_filled",
   "filled_qty": 0.2, "requested_qty": 0.5, "elapsed_seconds": 12,
   "timeout_seconds": 30}
`timeout_seconds` is optional — defaults to config's order_lifecycle.poll_timeout_seconds.

Output (JSON, to stdout):
  {"decision": "wait"|"proceed"|"rejected"|"timeout", "filled_qty": ..., "needs_cancel": ...,
   "symbol": ..., "order_id": ...}
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.alerts import send_alert
from lib.config import load_config
from lib.logging_utils import EventLogger
from lib.orders import evaluate_fill_status


def _load_input(input_arg: str) -> dict:
    if input_arg == "-":
        return json.load(sys.stdin)
    with open(input_arg) as f:
        return json.load(f)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default="-", help="JSON input file, or '-' for stdin (default)")
    parser.add_argument("--config", default="config/strategy.yaml")
    parser.add_argument("--data-dir", default="data")
    args = parser.parse_args()

    config = load_config(args.config)
    payload = _load_input(args.input)
    logger = EventLogger(args.data_dir)

    symbol = payload["symbol"]
    order_id = payload.get("order_id")
    timeout_seconds = payload.get("timeout_seconds", config["order_lifecycle"]["poll_timeout_seconds"])

    result = evaluate_fill_status(
        order_state=payload["order_state"],
        filled_qty=payload["filled_qty"],
        requested_qty=payload["requested_qty"],
        elapsed_seconds=payload["elapsed_seconds"],
        timeout_seconds=timeout_seconds,
    )

    logger.log(
        "order_fill_check",
        symbol=symbol,
        order_id=order_id,
        order_state=payload["order_state"],
        **result,
    )

    if result["decision"] in ("rejected", "timeout"):
        message = (
            f"Entry order for {symbol} (order_id={order_id}) ended as '{result['decision']}' "
            f"(state={payload['order_state']}, filled_qty={result['filled_qty']}). "
            f"needs_cancel={result['needs_cancel']}."
        )
        logger.log("entry_order_rejected_or_timeout", symbol=symbol, order_id=order_id, message=message)
        send_alert("entry_order_rejected_or_timeout", message)

    json.dump({**result, "symbol": symbol, "order_id": order_id}, sys.stdout)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
