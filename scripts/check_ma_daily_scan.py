#!/usr/bin/env python3
"""MA Pullback / Breakout-Retest agent's daily EOD scan (spec §2-3). Updates
breakout/retest tracking for every symbol currently being watched, plus
anything new off today's Finviz candidate list -- mirrors the reference
`daily_scan.run_daily_scan`'s union-of-finviz-and-tracked-symbols logic,
since a mid-retest symbol must keep being tracked even after it drops off a
fresh Finviz scan. Calls `lib.ma_breakout.update_watchlist_entry` per symbol.

**Places no orders.** This step never buys or sells anything -- it only
decides which symbols are eligible for `scripts/check_ma_hourly_signals.py`
to act on next.

Input (JSON, via --input file or stdin):
  {
    "finviz_candidates": [{"symbol": "AAPL", ...}, ...],   # today's Finviz Elite scan output, or bare symbol strings
    "price_history": {
      "AAPL": {"dates": [...], "high": [...], "low": [...], "close": [...], "volume": [...]},
      ...
    },   # oldest-first, ~2yr lookback for 252-day high + 200sma warmup
    "watchlist": { ...current data/ma_pullback/watchlist.json, or {} on a fresh setup... }
  }

Output (JSON, to stdout): the updated watchlist dict (same shape as
data/ma_pullback/watchlist.json) -- the caller persists it via
`StateStore.save_watchlist`.

Logs one `ma_breakout_tracked` (fresh breakout, including an overwrite of a
prior failed/aged-out entry), `ma_retest_seen` (retest_seen just flipped
True), or `ma_breakout_failed` (failed just flipped True) event per
meaningful state transition, plus `ma_watchlist_dropped` for a symbol aged
out or otherwise dropped, and one summary `ma_daily_scan` event per run.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.config import load_ma_config
from lib.logging_utils import EventLogger
from lib.ma_breakout import update_watchlist_entry
from lib.state import StateStore


def _load_input(input_arg: str) -> dict:
    if input_arg == "-":
        return json.load(sys.stdin)
    with open(input_arg) as f:
        return json.load(f)


def _candidate_symbol(c) -> str:
    return c["symbol"] if isinstance(c, dict) else c


def _bars_frame(bars_dict: dict) -> pd.DataFrame:
    dates = bars_dict["dates"]
    n = len(dates)
    return pd.DataFrame({
        "date": dates,
        "high": [float(x) for x in bars_dict["high"]],
        "low": [float(x) for x in bars_dict["low"]],
        "close": [float(x) for x in bars_dict["close"]],
        "volume": [float(x) for x in bars_dict.get("volume", [0] * n)],
    })


def run(payload: dict, config: dict, logger: EventLogger) -> dict:
    finviz_candidates = payload.get("finviz_candidates", [])
    price_history = payload.get("price_history", {})
    watchlist = dict(payload.get("watchlist") or {})

    finviz_symbols = {_candidate_symbol(c) for c in finviz_candidates}
    symbols_to_check = finviz_symbols | set(watchlist.keys())

    updated_watchlist = dict(watchlist)

    for symbol in sorted(symbols_to_check):
        bars_dict = price_history.get(symbol)
        if bars_dict is None:
            logger.log("ma_daily_scan_skipped", symbol=symbol, reason="no_price_history")
            continue

        bars = _bars_frame(bars_dict)
        prior_entry = watchlist.get(symbol)
        new_entry, eligible = update_watchlist_entry(prior_entry, bars, config)

        if new_entry is None:
            if prior_entry is not None:
                logger.log("ma_watchlist_dropped", symbol=symbol)
            updated_watchlist.pop(symbol, None)
            continue

        prior_breakout_date = (prior_entry or {}).get("breakout_date")
        if prior_entry is None or prior_breakout_date != new_entry.get("breakout_date"):
            logger.log(
                "ma_breakout_tracked",
                symbol=symbol,
                breakout_date=new_entry.get("breakout_date"),
                breakout_level=new_entry.get("breakout_level"),
            )
        else:
            if not (prior_entry or {}).get("retest_seen") and new_entry.get("retest_seen"):
                logger.log("ma_retest_seen", symbol=symbol, retest_low=new_entry.get("retest_low"))
            if not (prior_entry or {}).get("failed") and new_entry.get("failed"):
                logger.log("ma_breakout_failed", symbol=symbol, breakout_level=new_entry.get("breakout_level"))

        updated_watchlist[symbol] = new_entry

    logger.log(
        "ma_daily_scan",
        symbols_checked=sorted(symbols_to_check),
        watchlist_count=len(updated_watchlist),
        eligible_count=sum(1 for e in updated_watchlist.values() if e.get("eligible_for_entry")),
    )
    return updated_watchlist


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default="-", help="JSON input file, or '-' for stdin (default)")
    parser.add_argument("--config", default="config/ma_pullback_strategy.yaml")
    parser.add_argument("--data-dir", default="data/ma_pullback")
    args = parser.parse_args()

    config = load_ma_config(args.config)
    payload = _load_input(args.input)
    logger = EventLogger(args.data_dir)
    store = StateStore(args.data_dir)

    watchlist = run(payload, config, logger)

    store.save_watchlist(watchlist)

    json.dump(watchlist, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
