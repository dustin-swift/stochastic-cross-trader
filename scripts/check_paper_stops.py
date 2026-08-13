#!/usr/bin/env python3
"""Simulated stop-loss check for the daily-stochastic-check paper-trading
track (2026-08-13). The hourly (live) track's protective stop is a real
resting broker order, checked for a fill during reconciliation. The daily
track never places a real order at all (permanent dry-run, see
config/strategy_daily.yaml) -- there is no broker-side stop to reconcile
against, so this script simulates the same fill rule in code instead:
lib.backtest's documented convention (a stop fills at the exact stop price
on the first bar whose low touches or breaches it), applied to each open
paper position's most recent bar.

Deliberately narrow: this is ONLY the stop check, run before the ordinary
signal-exit check (mirrors lib.backtest's per-bar order, and the live
track's own "reconcile stop-outs first, then evaluate signal exits"
structure) -- a position that stops out here should never also be handed to
check_hourly_signals.py's exit evaluation for the same cycle.

Input (JSON, via --input file or stdin):
  {
    "positions": [
      {"symbol": "AAPL", "stop_price": 195.0,
       "bars": [{"begins_at": "...", "high": .., "low": .., "close": ..}, ...]}  # oldest-first; only the last bar matters
    ]
  }

Output (JSON, to stdout):
  {"stop_outs": [{"symbol": "AAPL", "exit_price": 195.0, "exit_time": "2026-08-13T00:00:00Z"}]}

Logs a `paper_stop_out` event per symbol that stopped out. A symbol with no
bars is skipped silently (nothing to check yet) rather than erroring.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.logging_utils import EventLogger


def _load_input(input_arg: str) -> dict:
    if input_arg == "-":
        return json.load(sys.stdin)
    with open(input_arg) as f:
        return json.load(f)


def find_stop_outs(positions: list[dict]) -> list[dict]:
    stop_outs = []
    for p in positions:
        bars = p.get("bars") or []
        if not bars:
            continue
        last_bar = bars[-1]
        stop_price = p["stop_price"]
        if stop_price is None:
            continue
        if float(last_bar["low"]) <= float(stop_price):
            stop_outs.append(
                {
                    "symbol": p["symbol"],
                    "exit_price": stop_price,
                    "exit_time": last_bar["begins_at"],
                }
            )
    return stop_outs


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", default="-", help="JSON input file, or '-' for stdin (default)")
    parser.add_argument("--data-dir", default="data")
    args = parser.parse_args()

    payload = _load_input(args.input)
    logger = EventLogger(args.data_dir)

    stop_outs = find_stop_outs(payload.get("positions", []))
    for so in stop_outs:
        logger.log("paper_stop_out", **so)

    json.dump({"stop_outs": stop_outs}, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
