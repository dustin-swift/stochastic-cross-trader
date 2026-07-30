#!/usr/bin/env python3
"""Run the historical backtest engine (lib.backtest) against already-fetched
data and report trades + summary stats. Research tool only — not part of the
live/dry-run pipeline, reads no live account state, places no orders.

Input (JSON, via --input file or stdin):
  {
    "symbol_data": {
      "AAPL": {
        "bars": [{"begins_at": "2026-07-20T14:00:00Z", "high": .., "low": .., "close": ..}, ...],  # oldest-first
        "atr_series": [{"begins_at": "2026-07-20T14:00:00Z", "value": ..}, ...]
      }, ...
    }
  }

Output (JSON, to stdout): the full result from lib.backtest.run_backtest
({"trades": [...], "open_positions": [...], "summary": {...}}). Also written
to <data-dir>/backtest_results.json and logged as a `backtest_run` event.

Usage:
  python3 scripts/run_backtest.py --input backtest_data.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.backtest import run_backtest
from lib.config import load_config
from lib.logging_utils import EventLogger


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
    symbol_data = payload["symbol_data"]

    result = run_backtest(symbol_data, config)

    out_path = Path(args.data_dir) / "backtest_results.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w") as f:
        json.dump(result, f, indent=2)

    logger = EventLogger(args.data_dir)
    logger.log(
        "backtest_run",
        symbols=sorted(symbol_data),
        trade_count=result["summary"]["trade_count"],
        open_position_count=len(result["open_positions"]),
        total_pnl=result["summary"]["total_pnl"],
        win_rate_pct=result["summary"]["win_rate_pct"],
    )

    json.dump(result, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
