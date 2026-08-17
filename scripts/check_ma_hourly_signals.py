#!/usr/bin/env python3
"""MA Pullback / Breakout-Retest agent's hourly job (spec §4-6), structurally
parallel to `check_hourly_signals.py`: evaluates exits FIRST for open
positions (via `lib.ma_signals.evaluate_exit`, daily bars), then evaluates
entries for eligible watchlist symbols (via `lib.ma_signals.evaluate_entry`,
daily + hourly bars). Pure computation over data the hourly-ma-signal-check
skill has already fetched -- this script places no orders itself.

Market-regime and earnings/catalyst vetoes are NOT applied here (matches
`lib.ma_signals.evaluate_entry`'s own docstring) -- those stay script-level
calls the skill makes before/after (`scripts/check_market_trend.py` against
`config/strategy.yaml`'s SPY signal, `scripts/filter_entry_earnings.py`),
same separation the existing hourly-signal-check skill uses.

Input (JSON, via --input file or stdin):
  {
    "open_positions": [
      {"symbol": "MSFT", "entry_price": 101.20, "stop_price": 98.50,
       "target_1": 105.25, "highest_close": 102.10, "bars_held": 3,
       "partial_taken": false, "atr_entry": 1.80, "shares": 37,
       "daily_bars": {"date": [...], "high": [...], "low": [...], "close": [...]}}
       # oldest-first, ending with the most recent completed daily bar
    ],
    "watchlist": {
      "AAPL": {"breakout_level": 97.32, "retest_low": 94.10, "retest_seen": true,
               "failed": false, "eligible_for_entry": true,
               "daily_bars": {...}, "hourly_bars": {"date": [...], "open": [...],
               "high": [...], "low": [...], "close": [...], "volume": [...]}}
       # only entries with eligible_for_entry=true are evaluated; a symbol
       # also present in open_positions is skipped (defense-in-depth --
       # shouldn't happen given lib.state's watchlist/positions separation)
    }
  }

Output (JSON, to stdout):
  {
    "entries": [{"symbol": "AAPL", "entry_price": ..., "stop_price": ..., "target_1": ...,
                 "shares": ..., "atr_entry": ..., "breakout_level": ..., "entry_date": ...}],
    "exits": [{"symbol": "MSFT", "action": "stop_hit"|"trend_invalidated"|"time_stop"|"partial",
               "position": {...updated position, see lib.ma_signals.evaluate_exit...}}],
    "position_updates": [{"symbol": "MSFT", "action": null|..., "position": {...}}],
    "stale_cycle": false, "cycle_gap_minutes": 62.3
  }

`exits` only contains positions with an actionable outcome this cycle
(a full exit, or a partial-profit sell). `position_updates` contains EVERY
open position checked this cycle, action or not -- the caller must persist
each one's `position` back onto `data/ma_pullback/positions.json` every
cycle, not just cycles with an exit, since `bars_held`/`highest_close`/a
ratcheted-up chandelier `stop_price` can all advance without a full exit
(mirrors the stochastic system's `position_states` convention for the same
reason: a state machine that isn't persisted every cycle silently resets).

Missed-cycle guard (mirrors `check_hourly_signals.py`'s pattern): this script
tracks its own last-completed-run time in `data/ma_pullback/last_cycle_at.json`.
If the gap since then exceeds `max_cycle_gap_minutes` (config, default 90 --
not a required config key, since a stale reclaim-trigger check is exactly as
dangerous here as a stale %K/%D comparison is on the stochastic system), any
entry that would otherwise fire this cycle is suppressed and logged as
`ma_entry_suppressed_stale_cycle` instead. Exits are never suppressed.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.config import load_ma_config
from lib.logging_utils import EventLogger
from lib.ma_signals import evaluate_entry, evaluate_exit
from lib.state import StateStore

_BAR_KEYS = ("daily_bars", "hourly_bars")
_NUMERIC_COLUMNS = ("open", "high", "low", "close", "volume")


def _load_input(input_arg: str) -> dict:
    if input_arg == "-":
        return json.load(sys.stdin)
    with open(input_arg) as f:
        return json.load(f)


def _frame(bars_dict: dict) -> pd.DataFrame:
    df = pd.DataFrame(dict(bars_dict))
    for col in _NUMERIC_COLUMNS:
        if col in df.columns:
            df[col] = df[col].astype(float)
    return df


def _strip_bars(entry: dict) -> dict:
    return {k: v for k, v in entry.items() if k not in _BAR_KEYS}


def run(
    payload: dict,
    config: dict,
    logger: EventLogger,
    now: datetime | None = None,
    last_cycle_at: datetime | None = None,
) -> dict:
    now = now or datetime.now(timezone.utc)
    max_cycle_gap_minutes = config.get("max_cycle_gap_minutes", 90)
    cycle_gap_minutes = None
    if last_cycle_at is not None:
        cycle_gap_minutes = (now - last_cycle_at).total_seconds() / 60
    stale_cycle = cycle_gap_minutes is not None and cycle_gap_minutes > max_cycle_gap_minutes

    # --- exits first ---
    exits = []
    position_updates = []
    open_symbols = set()
    for p in payload.get("open_positions", []):
        symbol = p["symbol"]
        open_symbols.add(symbol)

        daily_bars_dict = p.get("daily_bars")
        position = _strip_bars(p)
        if not daily_bars_dict or len(daily_bars_dict.get("close", [])) < 1:
            logger.log("ma_signal_check", symbol=symbol, kind="exit", skipped="insufficient_bars")
            position_updates.append({"symbol": symbol, "action": None, "position": position})
            continue

        daily_bars = _frame(daily_bars_dict)
        action, updated_position = evaluate_exit(position, daily_bars, config)

        logger.log(
            "ma_signal_check",
            symbol=symbol,
            kind="exit",
            action=action,
            stop_price=updated_position.get("stop_price"),
            bars_held=updated_position.get("bars_held"),
            partial_taken=updated_position.get("partial_taken"),
        )
        position_updates.append({"symbol": symbol, "action": action, "position": updated_position})
        if action is not None:
            exits.append({"symbol": symbol, "action": action, "position": updated_position})

    # --- entries (only eligible watchlist symbols, not already in a position) ---
    entries = []
    watchlist = payload.get("watchlist") or {}
    for symbol in sorted(watchlist.keys()):
        if symbol in open_symbols:
            continue

        entry = watchlist[symbol]
        if not entry.get("eligible_for_entry"):
            continue

        daily_bars_dict = entry.get("daily_bars")
        hourly_bars_dict = entry.get("hourly_bars")
        if not daily_bars_dict or not hourly_bars_dict:
            logger.log("ma_signal_check", symbol=symbol, kind="entry", skipped="missing_bars")
            continue

        daily_bars = _frame(daily_bars_dict)
        hourly_bars = _frame(hourly_bars_dict)
        watchlist_entry = _strip_bars(entry)

        decision = evaluate_entry(watchlist_entry, daily_bars, hourly_bars, config)

        if decision is None:
            logger.log("ma_signal_check", symbol=symbol, kind="entry", signal=False)
            continue

        if stale_cycle:
            logger.log(
                "ma_entry_suppressed_stale_cycle",
                symbol=symbol,
                cycle_gap_minutes=cycle_gap_minutes,
                max_cycle_gap_minutes=max_cycle_gap_minutes,
            )
            continue

        logger.log(
            "ma_signal_check",
            symbol=symbol,
            kind="entry",
            signal=True,
            entry_price=decision["entry_price"],
            stop_price=decision["stop_price"],
            shares=decision["shares"],
        )
        entries.append({"symbol": symbol, **decision})

    return {
        "entries": entries,
        "exits": exits,
        "position_updates": position_updates,
        "stale_cycle": stale_cycle,
        "cycle_gap_minutes": cycle_gap_minutes,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default="-", help="JSON input file, or '-' for stdin (default)")
    parser.add_argument("--config", default="config/ma_pullback_strategy.yaml")
    parser.add_argument("--data-dir", default="data/ma_pullback")
    parser.add_argument(
        "--now",
        default=None,
        help="Override current UTC datetime (ISO8601) -- for tests. Defaults to the real current time.",
    )
    parser.add_argument(
        "--last-cycle-at",
        default=None,
        help="Override the missed-cycle guard's prior-cycle timestamp (ISO8601), instead of reading "
        "data/ma_pullback/last_cycle_at.json directly -- for a sell-first cycle that calls this "
        "script twice, same reason as check_hourly_signals.py's --last-cycle-at flag.",
    )
    args = parser.parse_args()

    config = load_ma_config(args.config)
    payload = _load_input(args.input)
    logger = EventLogger(args.data_dir)
    now = datetime.fromisoformat(args.now.replace("Z", "+00:00")) if args.now else datetime.now(timezone.utc)

    store = StateStore(args.data_dir)
    if args.last_cycle_at:
        last_cycle_at = datetime.fromisoformat(args.last_cycle_at.replace("Z", "+00:00"))
    else:
        last_cycle_at_str = store.load_last_cycle_at()
        last_cycle_at = datetime.fromisoformat(last_cycle_at_str) if last_cycle_at_str else None

    result = run(payload, config, logger, now=now, last_cycle_at=last_cycle_at)

    store.save_last_cycle_at(now.isoformat())

    json.dump(result, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
