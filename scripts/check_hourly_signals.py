#!/usr/bin/env python3
"""Hourly signal check (spec §3-4): stochastic %K/%D entry signal for
candidates with an open slot, and exit signal for currently open positions.
Pure computation over data the hourly-signal-check skill has already fetched
via get_equity_historicals, plus each candidate's `atr14` carried straight
through from the Finviz export (see providers/finviz.py, scripts/
check_universe_screen.py) — no live indicator fetch for ATR. This script
places no orders itself.

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
       "pending": {"k_at_cross": 22.5},  # optional, see dual-cross entry note below; null/omitted if not pending
       "earnings_report_dates": [{"date": "2026-08-15", "timing": "am"}],  # optional, see catalyst-avoidance note below
       "bars": [{"high": 210.1, "low": 208.5, "close": 209.9}, ...]}   # oldest-first
    ],
    "open_positions": [
      {"symbol": "MSFT", "entry_price": 400.0, "qty": 0.25,
       "stochastic_state": "NORMAL",   # optional, defaults to NORMAL if omitted
       "earnings_report_dates": [{"date": "2026-07-30", "timing": "pm"}],  # optional, see catalyst-avoidance note below
       "bars": [{"high": ..., "low": ..., "close": ...}, ...]}
    ]
  }

Output (JSON, to stdout):
  {
    "entries": [{"symbol": "AAPL", "k": 24.1, "d": 19.8, "prev_k": 18.6, "prev_d": 15.2,
                 "atr14": 3.2, "estimated_stop_price": 202.1, "qty": 1}],
    "exits": [{"symbol": "MSFT", "k": 41.0, "d": 45.2, "prev_k": 48.3, "prev_d": 44.7,
               "stochastic_state": "NORMAL"}],
    "position_states": [{"symbol": "MSFT", "k": 41.0, "d": 45.2, "stochastic_state": "NORMAL"}],
    "candidate_states": [{"symbol": "AAPL", "k": 24.1, "d": 19.8, "pending": null}],
    "stale_cycle": false, "cycle_gap_minutes": 62.3
  }

`prev_k`/`prev_d` on both `entries[]` and `exits[]` (2026-08-05, at the user's
request for post-trade review data) are the PRIOR bar's %K/%D -- i.e. the
reading one hour before the bar that actually confirmed the entry/exit. This
is purely for later human review (trade_history.json persists both the
trigger-bar and prior-bar readings for every closed trade -- see
lib.state.close_trade_record) and doesn't feed into any decision here; `None`
whenever there weren't at least 2 valid bars to look back across, or for a
forced `earnings_exit` (which bypasses the oscillator check entirely, so
there's no k/d/prev_k/prev_d to report either).

`position_states` reports the (possibly just-updated) `stochastic_state` for
every open position checked this cycle, whether or not it exited — the skill
must write this back into positions.json every cycle so the state carries
forward, not just on cycles with an exit. `candidate_states` is the same idea
for candidates' `pending` field (see below) — write it back into
data/pending_entries.json every cycle, whether or not an entry fired.

Entry logic (spec §3, 2026-08-04 dual-cross revision — see
lib.signals.advance_pending_entry for the full state machine): entries no
longer fire the instant %K crosses above `oversold_threshold`. Both %K and
%D must cross above it, since %D — a slower, lagging average — is what
confirms a genuine sustained reversal rather than a brief %K spike. Because
%D lags, this is stateful across cycles: %K crossing starts a *pending*
setup (`{"k_at_cross": float}`, passed in/out via each candidate's
`pending` field) that persists until %D also crosses, %K falls back below
`oversold_threshold` (invalidated, no expiry timer needed), or %D confirms
too late with %K already past `stochastic.k_invalidate_max` (default 55 —
invalidated as too-extended, not a real reversal confirmation anymore).

Missed-cycle guard (2026-08-04, confirmed live on DNTH/ILF/PCAR — see `run`'s
inline comment for the full incident writeup): this script has no visibility
into wall-clock time between invocations other than what it tracks itself.
`main()` persists its own completion time to `data/last_cycle_at.json` (live
or dry-run, no skill-doc step needed for it) and passes the prior value into
`run(..., last_cycle_at=...)`. If the gap since then exceeds
`stochastic.max_cycle_gap_minutes` (default 90), any entry that would
otherwise fire this cycle is suppressed and logged as
`entry_suppressed_stale_cycle` instead — a scheduled routine that silently
skipped a cycle or two means the next successful run's two-bar crossing
check may span several real hours rather than one, satisfying the rule's
letter without the freshness it's meant to guarantee. `pending`/
`candidate_states` still advance normally either way; only whether an entry
actually fires this cycle is affected. Exits are never suppressed by this
guard.

Execution-lag cron fix (2026-08-04, confirmed live on CUZ): this script only
ever evaluates the last *fully closed* hourly bar (never a partial/forming
one) -- correct, but it means the signal it computes is only as fresh as
however long ago that bar closed. The hourly-signal-check cron used to fire
at :30 past each hour, so a signal computed off the bar that closed at the
top of the hour was already up to ~30 minutes stale by the time the routine
even started, on top of a few more minutes of routine-startup latency before
the order actually executed. Confirmed live: CUZ fired a bare, marginal
confirmation (%K=20.66, %D=22.14, barely over the 20 line) using the bar
that closed at 19:00 UTC; by the time the market order filled a few minutes
later, real-time price had already fallen enough that the very next bar
(19:00-20:00 UTC) recomputed to %K=12.15 -- back below the threshold, i.e.
the setup had already invalidated in the real market before the trade even
landed. The cron now fires at :02 past each hour instead of :30 (see
`config/strategy.yaml`'s `catalysts.forced_exit_utc_hour` comment for the
matching cron string), cutting that lag from ~30+ minutes down to ~2 -- this
doesn't eliminate the gap between "bar closes" and "order fills" entirely
(that's inherent to acting on any fully-closed bar), but it substantially
shrinks the window in which a marginal, barely-over-the-line confirmation
can reverse before the order lands.

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

Catalyst avoidance (lib.catalysts, config["catalysts"]["enabled"], default
true): `earnings_report_dates` is optional on both candidates and open
positions — each entry is either a `{"date": "YYYY-MM-DD", "timing": "am"|
"pm"}` dict (timing from get_earnings_results' report.timing; "am"=BMO,
"pm"=AMC) or a bare date string for backward compat (treated as the
conservative "am" default). The two cases are handled asymmetrically on
purpose:
- Candidates: if the field is present and today is exactly the report's
  BMO/AMC-aware exit_date (see lib.catalysts module docstring —
  `is_entry_blocked_day`), the candidate is skipped entirely (no entry, even
  if the oscillator would otherwise signal one) — entering on that day would
  get force-exited again almost immediately, a near-zero-value trade. This is
  deliberately a single-day buffer, not a multi-day window: entries on
  earlier days are allowed through so they still capture the run-up into the
  report. Absent field -> not re-checked here.
  **2026-08-06 architecture change**: the live hourly-signal-check skill no
  longer fetches earnings for every candidate up front (get_earnings_results
  has no batch mode, and only a handful of ~300+ candidates ever actually
  signal in a given cycle -- that per-candidate fetch was the dominant cost
  in the whole cycle's latency). Live cycles now pass candidates through
  this script with `earnings_report_dates` omitted entirely, then run
  `scripts/filter_entry_earnings.py` on the resulting `entries[]` shortlist
  -- fetching earnings only for symbols that already confirmed a signal,
  right before buying. This per-candidate field still works exactly as
  before for callers that already have the data cheaply in memory (chiefly
  `lib.backtest.py`, which fetches a symbol's full earnings history once for
  an entire backtest run, not per-cycle-per-symbol against a live API) --
  nothing here changed, only which callers still populate the field.
- Open positions: if present and today is on-or-after the nearest upcoming
  report's exit_date, the position is force-exited (reason "earnings_exit")
  regardless of stochastic state — including while OVERBOUGHT_HOLD, since a
  stock can easily be holding overbought right into its earnings date.
  **Gated to the last regular-session check of exit_date, not the first**
  (`is_forced_exit_day`'s `near_close` param, fixed 2026-08-04 — confirmed
  live on BALL: exiting on the first check of the day forfeited most of that
  day's run-up, exactly the outcome this rule exists to avoid). `near_close`
  is derived from `now` (real wall-clock UTC, or the optional `now` param for
  tests) compared against `config["catalysts"]["forced_exit_utc_hour"]`
  (default 19). A day already past exit_date (a missed close-of-day check)
  still force-exits immediately regardless of `near_close` — waiting further
  only adds gap risk. Absent field -> falls through to normal signal logic,
  NOT a forced exit — an existing position shouldn't get liquidated because a
  data fetch happened to fail this cycle; that's a materially more disruptive
  default than skipping a not-yet-opened candidate.

Trend-intact filter (config["trend_filter"], spec §4a, optional -- defaults
to enabled with a 50-bar SMA if the section is omitted entirely): stochastic
whipsaws repeatedly during a genuinely trending move, so a bearish %K/%D
crossover on a STATE_NORMAL position is suppressed whenever price is still
above its own SMA on the signal bar (see lib.indicators.sma, lib.signals
docstring). Computed fresh each cycle from the same bars already fetched for
the stochastic calc -- no extra data, no state field. STATE_OVERBOUGHT_HOLD
is completely unaffected (governed purely by the downside-80 rule); this
only ever gates the plain crossover exit.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.catalysts import is_entry_blocked_day, is_forced_exit_day
from lib.config import load_config
from lib.indicators import sma, stochastic
from lib.logging_utils import EventLogger
from lib.signals import STATE_NORMAL, STATE_OVERBOUGHT_HOLD, advance_pending_entry, exit_signal, stop_price, update_stochastic_state
from lib.sizing import entry_share_quantity
from lib.state import StateStore


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


def _trend_intact(bars: list[dict], sma_period: int) -> bool | None:
    """spec §4a: is price still above its own SMA on the last bar? None if
    there isn't enough bar history yet to compute the SMA -- exit_signal
    treats None the same as "unknown," falling through to the plain
    crossover check rather than trapping the position open on missing data.
    """
    df = pd.DataFrame(bars)[["close"]].astype(float)
    sma_series = sma(df, period=sma_period, column="close")
    latest_sma = sma_series.iloc[-1]
    if pd.isna(latest_sma):
        return None
    return bool(df["close"].iloc[-1] > latest_sma)


def run(
    payload: dict,
    config: dict,
    logger: EventLogger,
    today: date | None = None,
    now: datetime | None = None,
    last_cycle_at: datetime | None = None,
) -> dict:
    stoch_cfg = config["stochastic"]
    oversold = stoch_cfg["oversold_threshold"]
    overbought = stoch_cfg["overbought_threshold"]
    lookback_bars = stoch_cfg.get("entry_lookback_bars", 1)
    atr_mult = config["atr"]["stop_multiplier"]
    sizing_cfg = config["sizing"]
    catalysts_cfg = config.get("catalysts", {})
    catalysts_enabled = catalysts_cfg.get("enabled", True)
    # near_close (2026-08-04, spec §4b fix): the earnings force-exit must
    # only fire on the LAST regular-session check of the exit_date, not the
    # first -- see lib.catalysts.is_forced_exit_day's docstring. This
    # threshold is a config value, not a market-close lookup, because the
    # cron schedule that drives how often this script even runs is external
    # to it; forced_exit_utc_hour should match the UTC hour of the last
    # scheduled hourly-signal-check slot (default 19, i.e. the 19:02 UTC
    # slot on the current "2 13-19 * * 1-5" cron -- shifted from :30 to :02
    # past the hour 2026-08-04, see the execution-lag note below). Known limitation: the
    # cron is fixed UTC and doesn't shift for US daylight saving, so this
    # threshold's true proximity to the 4pm ET close can drift by an hour
    # across DST transitions -- same class of gap as the Finviz freshness
    # check's weekend-only handling (see providers/finviz.py), flagged
    # rather than silently assumed away.
    forced_exit_utc_hour = catalysts_cfg.get("forced_exit_utc_hour", 19)
    now = now or datetime.now(timezone.utc)
    near_close = now.hour >= forced_exit_utc_hour
    trend_filter_cfg = config.get("trend_filter", {})
    trend_filter_enabled = trend_filter_cfg.get("enabled", True)
    trend_filter_sma_period = trend_filter_cfg.get("sma_period", 50)
    today = today or now.date()

    k_invalidate_max = stoch_cfg.get("k_invalidate_max", 55)

    # Missed-cycle guard (2026-08-04, confirmed live on DNTH/ILF/PCAR): this
    # script has no idea how much real time has passed since it last ran --
    # if the scheduled routine silently skipped a cycle or two (confirmed
    # root cause that day: an MCP connector naming mismatch made two morning
    # runs abort before doing anything), the next successful run's two-bar
    # crossing check ends up comparing bars that are hours apart instead of
    # one hour apart. The crossing condition is still literally satisfied,
    # but by then %K/%D may have already been running well past 20 for a
    # while in the real market -- not the fresh reversal the rule is meant to
    # catch. `last_cycle_at` is this script's own record of when it last
    # completed (persisted to data/last_cycle_at.json by main(), live or
    # dry-run) -- if the gap since then exceeds max_cycle_gap_minutes
    # (default 90, i.e. 1.5x the normal hourly cadence), suppress firing any
    # entry this cycle (still update/persist pending state normally) rather
    # than trust a comparison that may span a real operational gap. Exits are
    # deliberately unaffected -- missing a chance to close risk is worse than
    # a late one, unlike opening new risk on a possibly-stale read.
    max_cycle_gap_minutes = stoch_cfg.get("max_cycle_gap_minutes", 90)
    cycle_gap_minutes = None
    if last_cycle_at is not None:
        cycle_gap_minutes = (now - last_cycle_at).total_seconds() / 60
    stale_cycle = cycle_gap_minutes is not None and cycle_gap_minutes > max_cycle_gap_minutes

    entries = []
    candidate_states = []
    for c in payload.get("candidates", []):
        symbol = c["symbol"]
        bars = c["bars"]
        prior_pending = c.get("pending")

        earnings_dates = c.get("earnings_report_dates")
        if catalysts_enabled and earnings_dates is not None and is_entry_blocked_day(earnings_dates, today):
            logger.log("signal_check", symbol=symbol, kind="entry", skipped="earnings_too_close")
            candidate_states.append({"symbol": symbol, "k": None, "d": None, "pending": prior_pending})
            continue

        if len(bars) < 2:
            logger.log("signal_check", symbol=symbol, kind="entry", skipped="insufficient_bars")
            candidate_states.append({"symbol": symbol, "k": None, "d": None, "pending": prior_pending})
            continue

        sdf = _stoch(bars, stoch_cfg)
        k, d = sdf["k"].iloc[-1], sdf["d"].iloc[-1]
        prev_k, prev_d = sdf["k"].iloc[-2], sdf["d"].iloc[-2]
        prev_k = None if pd.isna(prev_k) else float(prev_k)
        prev_d = None if pd.isna(prev_d) else float(prev_d)
        advance = advance_pending_entry(
            prior_pending,
            sdf,
            oversold_threshold=oversold,
            k_invalidate_max=k_invalidate_max,
            lookback_bars=lookback_bars,
        )
        signal = advance["signal"]
        new_pending = advance["pending"]

        logger.log(
            "signal_check",
            symbol=symbol,
            kind="entry",
            signal=signal,
            k=k,
            d=d,
            prev_k=prev_k,
            prev_d=prev_d,
            pending=new_pending,
        )
        candidate_states.append({"symbol": symbol, "k": k, "d": d, "pending": new_pending})

        if signal and stale_cycle:
            logger.log(
                "entry_suppressed_stale_cycle",
                symbol=symbol,
                k=k,
                d=d,
                cycle_gap_minutes=cycle_gap_minutes,
                max_cycle_gap_minutes=max_cycle_gap_minutes,
            )
            signal = False

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
                    "prev_k": prev_k,
                    "prev_d": prev_d,
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
        if catalysts_enabled and earnings_dates is not None and is_forced_exit_day(earnings_dates, today, near_close):
            logger.log(
                "signal_check",
                symbol=symbol,
                kind="exit",
                signal=True,
                exit_reason="earnings_exit",
                stochastic_state=prior_state,
            )
            position_states.append({"symbol": symbol, "k": None, "d": None, "stochastic_state": prior_state})
            exits.append({
                "symbol": symbol,
                "k": None,
                "d": None,
                "prev_k": None,
                "prev_d": None,
                "stochastic_state": prior_state,
                "exit_reason": "earnings_exit",
            })
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
        prev_k, prev_d = sdf["k"].iloc[-2], sdf["d"].iloc[-2]
        prev_k = None if pd.isna(prev_k) else float(prev_k)
        prev_d = None if pd.isna(prev_d) else float(prev_d)

        # State update happens BEFORE the exit check — a bar that newly spikes
        # both lines above the overbought threshold must already suppress a
        # same-bar crossover exit, not wait a cycle (spec §4).
        new_state = update_stochastic_state(prior_state, sdf, overbought_threshold=overbought)

        # Trend-intact filter (spec §4a): only meaningful for the NORMAL-state
        # crossover check — OVERBOUGHT_HOLD ignores it entirely inside
        # exit_signal — but it's cheap to always compute when enabled rather
        # than branch on state here too.
        trend_intact = _trend_intact(bars, trend_filter_sma_period) if trend_filter_enabled else None

        signal = exit_signal(sdf, state=new_state, overbought_threshold=overbought, trend_intact=trend_intact)

        logger.log(
            "signal_check",
            symbol=symbol,
            kind="exit",
            signal=signal,
            k=k,
            d=d,
            prev_k=prev_k,
            prev_d=prev_d,
            stochastic_state=new_state,
            state_transitioned=new_state != prior_state,
            trend_intact=trend_intact,
        )

        position_states.append({"symbol": symbol, "k": k, "d": d, "stochastic_state": new_state})
        if signal:
            reason = "overbought_hold_exit" if new_state == STATE_OVERBOUGHT_HOLD else "signal_exit"
            exits.append({
                "symbol": symbol,
                "k": k,
                "d": d,
                "prev_k": prev_k,
                "prev_d": prev_d,
                "stochastic_state": new_state,
                "exit_reason": reason,
            })

    return {
        "entries": entries,
        "exits": exits,
        "position_states": position_states,
        "candidate_states": candidate_states,
        "stale_cycle": stale_cycle,
        "cycle_gap_minutes": cycle_gap_minutes,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default="-", help="JSON input file, or '-' for stdin (default)")
    parser.add_argument("--config", default="config/strategy.yaml")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument(
        "--now",
        default=None,
        help="Override current UTC datetime (ISO8601, e.g. 2026-08-04T20:00:00Z) -- "
        "for tests that need to pin the earnings near_close gate. Defaults to the real current time.",
    )
    parser.add_argument(
        "--last-cycle-at",
        default=None,
        help="Override the missed-cycle guard's prior-cycle timestamp (ISO8601), instead of "
        "reading data/last_cycle_at.json directly -- for a sell-first cycle (2026-08-04) that "
        "calls this script twice, once for exits (candidates: []) and, only if a slot opened up, "
        "once more for entries: the exits call already overwrites last_cycle_at.json with THIS "
        "cycle's own timestamp, so the entries call needs the true prior value passed in "
        "explicitly rather than reading a file the first call just touched. This script always "
        "writes the new value at the end regardless of which source was used to read the old one.",
    )
    args = parser.parse_args()

    config = load_config(args.config)
    payload = _load_input(args.input)
    logger = EventLogger(args.data_dir)
    now = datetime.fromisoformat(args.now.replace("Z", "+00:00")) if args.now else datetime.now(timezone.utc)

    # Missed-cycle guard (2026-08-04, see run()'s docstring comment): this
    # script tracks its own last-completed-run timestamp directly in data/,
    # the same way EventLogger writes logs directly rather than routing
    # through the skill/agent -- no extra skill-doc step needed to keep it
    # current, except when --last-cycle-at overrides it (see above).
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
