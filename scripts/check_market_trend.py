#!/usr/bin/env python3
"""Market-regime check (2026-08-06, at the user's direction). Thin CLI
wrapping lib.market_filter.market_trend_intact — same shape as
check_circuit_breaker.py. The hourly-signal-check skill calls this each
cycle before evaluating any new entries (section 5); a not-trending market
skips new entries entirely for the cycle, the same way a tripped circuit
breaker does. Exits and existing positions are never affected.

Input (JSON, via --input file or stdin):
  {"bars": [{"high": .., "low": .., "close": ..}, ...]}   # bars, oldest-first, for config["market_filter"]["symbol"]

Granularity-agnostic -- this script just runs whatever bars it's handed
through lib.market_filter.market_trend_intact with config["market_filter"]'s
fast/slow SMA periods. The deployed config uses MINUTE bars (2026-08-06
revision, at the user's direction -- see lib/market_filter.py's module
docstring for why: an hourly 20/200 filter can't actually catch a steep
single-day decline inside an otherwise-intact longer uptrend, since a 3-day
average rarely dips below a 6-week one; minute bars turn this into a
same-day momentum read instead of a multi-week regime read), with
`require_rising: false` (a plain fast-above-slow comparison, no slope check
— minute-level averages are noisy enough that a separate rising requirement
adds little).

Output (JSON, to stdout): {"trend_intact": bool | null}
`null` means there wasn't enough bar history to evaluate -- treat this the
same as `true` (don't block entries on missing data, same convention as the
exit-side trend_intact filter).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.config import load_config
from lib.logging_utils import EventLogger
from lib.market_filter import market_trend_intact


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

    mf_cfg = config.get("market_filter", {})
    fast_period = mf_cfg.get("fast_sma_period", 20)
    slow_period = mf_cfg.get("slow_sma_period", 200)
    rising_lookback_bars = mf_cfg.get("rising_lookback_bars", 5)
    require_rising = mf_cfg.get("require_rising", True)
    symbol = mf_cfg.get("symbol", "SPY")

    if not mf_cfg.get("enabled", True):
        # Disabled entirely -- True (not None) so this reads unambiguously
        # as "not blocking," distinct from "couldn't determine."
        logger.log("market_trend_check", symbol=symbol, trend_intact=True, enabled=False)
        json.dump({"trend_intact": True}, sys.stdout)
        sys.stdout.write("\n")
        return

    bars = payload["bars"]
    trend_intact = market_trend_intact(
        bars,
        fast_period=fast_period,
        slow_period=slow_period,
        rising_lookback_bars=rising_lookback_bars,
        require_rising=require_rising,
    )

    logger.log(
        "market_trend_check",
        symbol=symbol,
        trend_intact=trend_intact,
        fast_sma_period=fast_period,
        slow_sma_period=slow_period,
        rising_lookback_bars=rising_lookback_bars,
        require_rising=require_rising,
    )

    json.dump({"trend_intact": trend_intact}, sys.stdout)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
