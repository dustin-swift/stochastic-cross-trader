#!/usr/bin/env python3
"""Settles queued daily-stochastic-check paper fills at the next trading
day's OPEN price (2026-08-14, at the user's direction -- see
scripts/queue_paper_fill.py for why same-day-close fills were replaced with
this next-open scheme). Run once per cycle, before anything else that reads
positions.json -- a symbol settled this cycle should immediately count
against slot availability and be eligible for exit evaluation the same
cycle, exactly like a live entry filling mid-cycle would.

Input (JSON, via --input file or stdin):
  {"bars_by_symbol": {"AAPL": [{"begins_at": "...", "open": 210.0, "high": .., "low": .., "close": ..}, ...]}}
  # oldest-first; only the LAST bar's "open" is used. A symbol from
  # data/pending_fills.json with no entry here (fetch failed, or genuinely
  # nothing new yet) is left pending and retried next cycle -- NOT dropped.

Pending fills themselves are read from `--data-dir`'s pending_fills.json,
not from the input payload -- this script is the only writer of that file
besides scripts/queue_paper_fill.py, so reading it directly avoids the
caller having to round-trip it through the input payload.

Output (JSON, to stdout): {"filled": [{"symbol", "entry_price", "qty", "stop_price"}, ...], "still_pending": [symbols...]}

For each symbol that settles: sizes qty and the ATR stop off the ACTUAL
open price (not the signal-time estimate) -- mirrors how the live
hourly track's entries always compute the real stop from the real fill
price, not the signal-bar's price (see check_hourly_signals.py's
`estimated_stop_price` naming and hourly-signal-check.md section 5 step 3).
A symbol whose open now exceeds `sizing.max_price_per_share` is dropped
entirely (logged `paper_fill_skipped_price_cap`), not retried -- the
opportunity is simply not taken, same convention as a live
`entry_skipped_price_cap`.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.config import load_config
from lib.logging_utils import EventLogger
from lib.signals import stop_price
from lib.sizing import entry_share_quantity
from lib.state import StateStore


def _load_input(input_arg: str) -> dict:
    if input_arg == "-":
        return json.load(sys.stdin)
    with open(input_arg) as f:
        return json.load(f)


def settle_paper_fills(
    bars_by_symbol: dict[str, list[dict]],
    store: StateStore,
    logger: EventLogger,
    per_trade_usd: float,
    max_price_per_share: float,
    atr_stop_multiplier: float,
) -> dict:
    pending = store.load_pending_fills()
    positions = store.load_positions()

    filled = []
    still_pending = {}
    for symbol, info in pending.items():
        bars = bars_by_symbol.get(symbol)
        if not bars:
            still_pending[symbol] = info
            continue

        open_price = float(bars[-1]["open"])
        qty = entry_share_quantity(open_price, per_trade_usd, max_price_per_share)
        if qty is None:
            logger.log("paper_fill_skipped_price_cap", symbol=symbol, open_price=open_price, max_price_per_share=max_price_per_share)
            continue

        atr14 = info.get("atr14")
        stop = stop_price(open_price, atr14, mult=atr_stop_multiplier) if atr14 is not None else None

        position = {
            "entry_price": open_price,
            "qty": qty,
            "entry_time": bars[-1]["begins_at"],
            "entry_order_id": "paper",
            "stop_order_id": None,
            "stop_price": stop,
            "stochastic_state": "NORMAL",
            "entry_k": info.get("entry_k"),
            "entry_d": info.get("entry_d"),
            "entry_prev_k": info.get("entry_prev_k"),
            "entry_prev_d": info.get("entry_prev_d"),
        }
        positions[symbol] = position
        filled.append({"symbol": symbol, "entry_price": open_price, "qty": qty, "stop_price": stop})
        logger.log(
            "paper_entry_filled_next_open",
            symbol=symbol,
            entry_price=open_price,
            qty=qty,
            stop_price=stop,
            signal_date=info.get("signal_date"),
        )

    store.save_positions(positions)
    store.save_pending_fills(still_pending)
    return {"filled": filled, "still_pending": list(still_pending.keys())}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", default="-", help="JSON input file, or '-' for stdin (default)")
    parser.add_argument("--config", default="config/strategy_daily.yaml")
    parser.add_argument("--data-dir", default="data/daily")
    args = parser.parse_args()

    payload = _load_input(args.input)
    config = load_config(args.config)
    store = StateStore(args.data_dir)
    logger = EventLogger(args.data_dir)

    result = settle_paper_fills(
        payload.get("bars_by_symbol", {}),
        store,
        logger,
        per_trade_usd=config["sizing"]["per_trade_usd"],
        max_price_per_share=config["sizing"]["max_price_per_share"],
        atr_stop_multiplier=config["atr"]["stop_multiplier"],
    )

    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
