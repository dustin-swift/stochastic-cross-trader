#!/usr/bin/env python3
"""Hourly signal check (spec §3-4): stochastic %K/%D entry signal for
candidates with an open slot, and exit signal for currently open positions.
Pure computation over data the hourly-signal-check skill has already fetched
via get_equity_historicals (+ get_equity_technical_indicators for ATR on
new-entry candidates) — this script places no orders itself.

Exit signal is a per-position state machine (spec §4 overbought-hold
refinement, see lib.signals): while `stochastic_state` is NORMAL, exit is an
ordinary bearish %K/%D crossover; once %K and %D have both been >= the
overbought threshold together, the position moves to OVERBOUGHT_HOLD and
crossovers are ignored until both lines drop back below the threshold. The
caller (hourly-signal-check skill) is responsible for persisting the returned
`stochastic_state` on each position between cycles — this script is stateless
per invocation, it only advances the state by one bar given what it's handed.

Input (JSON, via --input file or stdin):
  {
    "candidates": [
      {"symbol": "AAPL", "sector": "Technology", "atr14": 3.2,
       "earnings_report_dates": ["2026-08-15"],  # optional, see catalyst-avoidance note below
       "bars": [{"high": 210.1, "low": 208.5, "close": 209.9}, ...]}   # oldest-first
    ],
    "open_positions": [
      {"symbol": "MSFT", "entry_price": 400.0, "qty": 0.25,
       "stochastic_state": "NORMAL",   # optional, defaults to NORMAL if omitted
       "earnings_report_dates": ["2026-07-30"],  # optional, see catalyst-avoidance note below
       "bars": [{"high": ..., "low": ..., "close": ...}, ...]}
    ]
  }

Output (JSON, to stdout):
  {
    "entries": [{"symbol": "AAPL", "k": 24.1, "d": 19.8, "atr14": 3.2,
                 "estimated_stop_price": 202.1, "qty": 1}],
    "exits": [{"symbol": "MSFT", "k": 41.0, "d": 45.2, "stochastic_state": "NORMAL"}],
    "position_states": [{"symbol": "MSFT", "k": 41.0, "d": 45.2, "stochastic_state": "NORMAL"}]
  }

`position_states` reports the (possibly just-updated) `stochastic_state` for
every open position checked this cycle, whether or not it exited — the skill
must write this back into positions.json every cycle so the state carries
forward, not just on cycles with an exit.

`entries[].qty` (spec update 2026-07-30, see lib.sizing): a whole-share
quantity, sized off the last bar's close via lib.sizing.entry_share_quantity
(config["sizing"]) -- entries are no longer dollar-based fractional orders,
since this broker rejects any stop-type order against a fractional-share
quantity. A candidate whose last close exceeds config["sizing"]["max_price_per_share"]
never makes it into `entries` at all -- it's skipped and logged as
`entry_skipped_price_cap` instead, even though the oscillator signal fired.

`estimated_stop_price` uses the last bar's close as a stand-in entry price —
the skill recomputes the real stop from the actual fill price once the market
buy executes (see plan: Order lifecycle).

Catalyst avoidance (lib.catalysts): `earnings_report_dates` is optional on
both candidates and open positions, and the two cases are handled
asymmetrically on purpose:
- Candidates: if the field is present and earnings are within
  config["catalysts"]["earnings_exclusion_days"], the candidate is skipped
  entirely (no entry, even if the oscillator would otherwise signal one).
  Absent field -> not re-checked here (candidates.json is already earnings-
  filtered by the daily screen when that ran with --earnings-input; this is a
  defense-in-depth re-check, not the primary gate).
- Open positions: if present and earnings are within
  config["catalysts"]["earnings_forced_exit_days"], the position is force-
  exited (reason "earnings_exit") regardless of stochastic state. Absent
  field -> falls through to normal signal logic, NOT a forced exit — an
  existing position shouldn't get liquidated because a data fetch happened
  to fail this cycle; that's a materially more disruptive default than
  skipping a not-yet-opened candidate.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.catalysts import is_too_close_to_earnings
from lib.config import load_config
from lib.indicators import stochastic
from lib.logging_utils import EventLogger
from lib.signals import STATE_NORMAL, STATE_OVERBOUGHT_HOLD, entry_signal, exit_signal, stop_price, update_stochastic_state
from lib.sizing import entry_share_quantity


def _load_input(input_arg: str) -> dict:
    if input_arg == "-":
        return json.load(sys.stdin)
    with open(input_arg) as f:
        return json.load(f)


def _stoch(bars: list[dict], stoch_cfg: dict) -> pd.DataFrame:
    # Robinhood's get_equity_historicals returns high/low/close as JSON
    # strings (e.g. "253.595000"), not numbers — coerce before handing to
    # pandas, otherwise the rolling min/max/subtraction in lib.indicators
    # blows up on string operands.
    df = pd.DataFrame(bars)[["high", "low", "close"]].astype(float)
    return stochastic(
        df,
        k_period=stoch_cfg["k_period"],
        k_smooth=stoch_cfg["k_smooth"],
        d_period=stoch_cfg["d_period"],
    )


def run(payload: dict, config: dict, logger: EventLogger, today: date | None = None) -> dict:
    stoch_cfg = config["stochastic"]
    oversold = stoch_cfg["oversold_threshold"]
    overbought = stoch_cfg["overbought_threshold"]
    lookback_bars = stoch_cfg.get("entry_lookback_bars", 1)
    atr_mult = config["atr"]["stop_multiplier"]
    sizing_cfg = config["sizing"]
    catalysts_cfg = config.get("catalysts", {})
    earnings_exclusion_days = catalysts_cfg.get("earnings_exclusion_days", 5)
    earnings_forced_exit_days = catalysts_cfg.get("earnings_forced_exit_days", 1)
    today = today or date.today()

    entries = []
    for c in payload.get("candidates", []):
        symbol = c["symbol"]
        bars = c["bars"]

        earnings_dates = c.get("earnings_report_dates")
        if earnings_dates is not None and is_too_close_to_earnings(earnings_dates, today, earnings_exclusion_days):
            logger.log("signal_check", symbol=symbol, kind="entry", skipped="earnings_too_close")
            continue

        if len(bars) < 2:
            logger.log("signal_check", symbol=symbol, kind="entry", skipped="insufficient_bars")
            continue

        sdf = _stoch(bars, stoch_cfg)
        k, d = sdf["k"].iloc[-1], sdf["d"].iloc[-1]
        signal = entry_signal(sdf, oversold_threshold=oversold, lookback_bars=lookback_bars)

        logger.log("signal_check", symbol=symbol, kind="entry", signal=signal, k=k, d=d)

        if signal:
            last_close = float(bars[-1]["close"])
            atr14 = c.get("atr14")
            estimated_stop = stop_price(last_close, atr14, mult=atr_mult) if atr14 is not None else None

            # Whole-share sizing (2026-07-30): a fractional-share fill can't
            # get a resting stop placed at all on this broker (confirmed
            # live). qty is decided here, off the last bar's close, not at
            # fill time -- close enough for the price-cap decision and qty
            # count; the real stop still gets computed from the actual fill
            # price after the order executes, unchanged.
            qty = entry_share_quantity(last_close, sizing_cfg["per_trade_usd"], sizing_cfg["max_price_per_share"])
            if qty is None:
                logger.log(
                    "entry_skipped_price_cap",
                    symbol=symbol,
                    last_close=last_close,
                    max_price_per_share=sizing_cfg["max_price_per_share"],
                )
                continue

            entries.append(
                {
                    "symbol": symbol,
                    "sector": c.get("sector"),
                    "k": k,
                    "d": d,
                    "atr14": atr14,
                    "last_close": last_close,
                    "estimated_stop_price": estimated_stop,
                    "qty": qty,
                }
            )

    exits = []
    position_states = []
    for p in payload.get("open_positions", []):
        symbol = p["symbol"]
        bars = p["bars"]
        prior_state = p.get("stochastic_state") or STATE_NORMAL

        earnings_dates = p.get("earnings_report_dates")
        if earnings_dates is not None and is_too_close_to_earnings(earnings_dates, today, earnings_forced_exit_days):
            logger.log(
                "signal_check",
                symbol=symbol,
                kind="exit",
                signal=True,
                exit_reason="earnings_exit",
                stochastic_state=prior_state,
            )
            position_states.append({"symbol": symbol, "k": None, "d": None, "stochastic_state": prior_state})
            exits.append({"symbol": symbol, "k": None, "d": None, "stochastic_state": prior_state, "exit_reason": "earnings_exit"})
            continue  # forced exit overrides the normal oscillator check entirely for this cycle

        if len(bars) < 2:
            logger.log(
                "signal_check",
                symbol=symbol,
                kind="exit",
                skipped="insufficient_bars",
                stochastic_state=prior_state,
            )
            # Can't advance the state without a stochastic value, but still
            # report it so the caller doesn't drop the field on a skipped bar.
            position_states.append({"symbol": symbol, "k": None, "d": None, "stochastic_state": prior_state})
            continue

        sdf = _stoch(bars, stoch_cfg)
        k, d = sdf["k"].iloc[-1], sdf["d"].iloc[-1]

        # State update happens BEFORE the exit check — a bar that newly spikes
        # both lines above the overbought threshold must already suppress a
        # same-bar crossover exit, not wait a cycle (spec §4).
        new_state = update_stochastic_state(prior_state, sdf, overbought_threshold=overbought)
        signal = exit_signal(sdf, state=new_state, overbought_threshold=overbought)

        logger.log(
            "signal_check",
            symbol=symbol,
            kind="exit",
            signal=signal,
            k=k,
            d=d,
            stochastic_state=new_state,
            state_transitioned=new_state != prior_state,
        )

        position_states.append({"symbol": symbol, "k": k, "d": d, "stochastic_state": new_state})
        if signal:
            reason = "overbought_hold_exit" if new_state == STATE_OVERBOUGHT_HOLD else "signal_exit"
            exits.append({"symbol": symbol, "k": k, "d": d, "stochastic_state": new_state, "exit_reason": reason})

    return {"entries": entries, "exits": exits, "position_states": position_states}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default="-", help="JSON input file, or '-' for stdin (default)")
    parser.add_argument("--config", default="config/strategy.yaml")
    parser.add_argument("--data-dir", default="data")
    args = parser.parse_args()

    config = load_config(args.config)
    payload = _load_input(args.input)
    logger = EventLogger(args.data_dir)

    result = run(payload, config, logger)

    json.dump(result, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
