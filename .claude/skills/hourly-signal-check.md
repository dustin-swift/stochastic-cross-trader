---
name: hourly-signal-check
description: Run hourly during market hours. Reconciles open positions, checks the daily-loss circuit breaker, evaluates exits FIRST (state-aware — see the §4 overbought-hold refinement and §4a trend-intact filter — plus earnings/catalyst avoidance), then re-checks slot availability before evaluating the stochastic entry signal (dual %K/%D-cross-20 confirmation, spec §3 2026-08-04 revision) so a slot freed by a same-cycle exit can be used the same cycle (2026-08-04 fix), and (only if config live=true) places real orders with a resting ATR stop.
---

# Hourly Signal Check

Spec: `~/Downloads/hourly-stochastic-strategy-spec (1).md` §3-5, §6b. Plan: `/Users/dustinrowley/.claude/plans/elegant-brewing-phoenix.md`.

**Read `config/strategy.yaml` first.** Every threshold and the `live` flag live
there — this doc describes the procedure, not the numbers. If `live: false`
(the default), this run computes and logs everything but **must not call any
order-placing or order-cancelling tool**. Treat that as a hard rule, not a
suggestion — dry-run is the whole point of the flag.

## 0. Setup

This routine runs in an isolated cloud checkout with no memory of previous
runs and no access to the user's Mac. All persistent state (`data/` —
`positions.json`, `candidates.json`, `finviz_export.csv`, logs) lives on a
dedicated `bot-state` branch, not in the routine's working tree by default.

- **Sync state in (mandatory, do this first)**: `bash scripts/sync_state.sh pull` — populates `data/` from the `bot-state` branch. If it reports no `bot-state` branch yet, continue with an empty `data/`: `positions.json`/`candidates.json` will simply read as empty/missing, which is the correct "nothing open, nothing to trade" starting state for a genuinely fresh setup.
- `account_number` = `config/strategy.yaml`'s `account_number`.
- Load `data/positions.json`, `data/candidates.json`, and `data/pending_entries.json` (read the files directly, or run a one-off `python3 -c "from lib.state import StateStore; import json; print(json.dumps(StateStore().load_positions()))"` — either is fine). `pending_entries.json` (spec §3, 2026-08-04 dual-cross revision) holds `{symbol: {"k_at_cross": float}}` for any candidate whose %K has crossed above the oversold threshold but is still waiting on %D to also cross — reset to `{}` each morning by the daily screen, so it never carries a setup across trading days. `data/last_cycle_at.json` (missed-cycle guard, see section 2's last bullet and section 5) is read/written directly by `check_hourly_signals.py` itself — nothing to do with it here beyond the normal state sync in/out.

## 1. Reconcile first

Before evaluating any new signal, check whether reality has moved since the last cycle:

- For each symbol in `positions.json` with a `stop_order_id`: `get_equity_orders(account_number, order_id=stop_order_id)`. If `state=filled`, the position was **stopped out** at the broker since the last cycle (a resting order can fill any time, not just when this skill runs).
  - **Record the closed trade before clearing the slot**, using the order record's average fill price/time as the exit:
    ```bash
    echo '{"symbol": "...", "position": <the positions.json entry for this symbol, unmodified>, "exit_price": <average_price from the order record>, "exit_time": <fill time from the order record>, "exit_order_id": "<stop_order_id>", "exit_reason": "stop_out"}' | python3 scripts/record_trade_close.py
    ```
    This appends to `data/trade_history.json` and logs `trade_closed` — this is the *only* durable record of a closed trade once it's gone from `positions.json`, so don't skip it even though the position is about to be removed anyway.
  - Clear that symbol's slot from `positions.json` (rewrite the file — atomic write, see `lib/state.py`).
  - Log a `stop_out` event: symbol, fill price/time from the order record.
- Cross-check against `get_equity_positions(account_number)` for anything in `positions.json` that no longer has a matching real position (or vice versa) — log a `reconciliation_drift` event if you find a mismatch (e.g. a manual trade placed outside this system) but don't guess at a fix; surface it.

## 2. Circuit breaker check

- `get_portfolio(account_number)` → account value, unrealized P&L.
- `get_realized_pnl(account_number, span="day", asset_classes=["equity"])` → today's realized P&L. Pass `asset_classes` explicitly — omitting it errors with "un-specified asset class" in practice, despite the tool description saying it's optional (confirmed during the first dry run).
- `echo '{"account_value": ..., "realized_pnl_today": ..., "unrealized_pnl_today": ...}' | python3 scripts/check_circuit_breaker.py` → `{"tripped": bool}`. This already logs `circuit_breaker_check` (always) and `circuit_breaker_triggered` + sends an alert (only if tripped) — don't duplicate that logging yourself.
- If `tripped`: **skip all new-entry evaluation for the rest of this run** (section 5 below). Exit/stop-out handling (sections 1 and 3) still runs normally — the breaker only blocks opening new risk, it doesn't touch existing positions.
- **Capture the missed-cycle guard's prior timestamp now, before section 3 runs**: read `data/last_cycle_at.json`'s current value (e.g. `python3 -c "from lib.state import StateStore; print(StateStore().load_last_cycle_at() or '')"`). Call this `prior_cycle_at` — you'll need it in section 5. This has to happen *before* section 3's exits call, because that call overwrites `data/last_cycle_at.json` with this cycle's own timestamp (see section 5 for why that matters). If the file doesn't exist yet, `prior_cycle_at` is empty/None — that's fine, it just means no missed-cycle guard applies this cycle (matches a genuinely fresh setup).

## 3. Exits first — bearish %K/%D crossover, overbought-hold reversal, or earnings-forced

**Exits run before slot availability is checked (2026-08-04 fix)**: a position sold this cycle frees a slot that a fresh entry signal should be able to use in the *same* cycle, not the next one — confirmed live: a cycle found 15/15 slots full at the old "determine slot availability" step, skipped candidate evaluation entirely, then went on to sell 3 positions on signal exits, ending the cycle with 3 open slots and zero entries evaluated against them. Running exits first (and rechecking slot availability afterward, section 4) fixes that.

This step only ever needs `open_positions` bars — no candidates involved, so it's a self-contained fetch-and-run:

- For every symbol in `positions.json`, `get_equity_historicals(symbols=[...], interval="hour", start_time=<enough lookback for k_period+k_smooth+d_period bars — a few trading days back is plenty>)`. Batch up to 10 symbols per call. Check the `interpolated` flag on returned bars — real intraday data has historically covered roughly the trailing 2-4 weeks in this environment; if a symbol's recent bars come back `interpolated=true`, skip it and log why rather than feeding synthetic data into the signal.
- **Earnings/catalyst check** (see `lib/catalysts.py` and the daily-screen skill for why this exists): `get_earnings_results(symbol=...)` for every symbol in `positions.json` — a **fresh** check every cycle, since a position can be held for several days after the daily screen last looked at it and drift into an earnings date that was safely far away at entry time. Build the report list per symbol from the response as `{"date": report.date, "timing": report.timing}` objects (past dates are harmless to include, `lib.catalysts` ignores them) — **include `timing`** ("am"=BMO, "pm"=AMC), since omitting it silently falls back to the conservative BMO default and can cost a day of run-up on AMC names or force-close a day earlier than necessary. If a fetch fails for a symbol, simply omit `earnings_report_dates` for it — the position falls back to normal signal logic (deliberately **not** a forced exit — see script docstring, a data-fetch hiccup shouldn't liquidate a live position).
- Build the payload with `"candidates": []` (nothing to evaluate for entry yet — that's section 5) and `"open_positions"` populated. For each open position, pass its current `stochastic_state` from `positions.json` (a position written before this field existed, or one you're testing from scratch, has no such key — omit it and the script defaults to `NORMAL`, so nothing needs a migration):
  ```json
  {
    "candidates": [],
    "open_positions": [{"symbol": "MSFT", "entry_price": 400.0, "qty": 0.25, "stochastic_state": "NORMAL", "earnings_report_dates": [], "bars": [...]}]
  }
  ```
  Bars must be oldest-first.
- Run it: `echo '<payload>' | python3 scripts/check_hourly_signals.py` → `{"entries": [], "exits": [...], "position_states": [...], "candidate_states": [], "stale_cycle": false, "cycle_gap_minutes": null}` (`entries`/`candidate_states` are trivially empty since `candidates` was empty — the missed-cycle guard is irrelevant to this call, don't pass `--last-cycle-at`, let it use its normal default read/write). This call also logs a `signal_check` event per symbol regardless of `live`; an earnings-forced exit shows up in `exits` with `"exit_reason": "earnings_exit"`.

  **This call is also what advances the missed-cycle guard's bookkeeping for the whole cycle** (writes `data/last_cycle_at.json` = now) — it runs every cycle unconditionally, so `last_cycle_at.json` always reflects "the routine successfully ran," independent of whether any candidates ever get evaluated this cycle (e.g. several consecutive cycles with 0 open slots). Don't add a second write anywhere else in this skill for it.
- **Persist `position_states` immediately**, before doing anything else with the result: for every entry in `position_states`, write its `stochastic_state` back onto the matching symbol in `positions.json` (atomic write). Do this on **every** cycle, not just cycles with an exit — a position that just transitioned into `OVERBOUGHT_HOLD` (or is still there) needs that recorded even when nothing else changes, otherwise the next cycle would incorrectly start it back at `NORMAL` and the whole point of the state machine (spec §4) is lost.

The `exits` list already reflects the state-aware logic (spec §4, §4a): a position in `NORMAL` state exits on an ordinary bearish %K/%D crossover — **unless price is still above its own `trend_filter.sma_period`-bar SMA on the signal bar** (default 50, spec §4a), in which case the crossover is suppressed and the position stays open through the whipsaw. A position that reached `OVERBOUGHT_HOLD` (both %K and %D simultaneously >= `stochastic.overbought_threshold`, default 80) ignores crossovers entirely regardless of the trend filter — even repeated whipsaws between the two lines — and exits only once both lines drop back below that threshold. A third case, `exit_reason: "earnings_exit"`, fires regardless of stochastic state — including mid-`OVERBOUGHT_HOLD` — whenever today is on or after a held position's next known report's BMO/AMC-aware exit_date (see `lib/catalysts.py`: D-1 for a before-market-open report, D itself for after-market-close) — this one is a scheduled risk decision, not a signal read, so don't second-guess it against the k/d values even if they look fine. **On the exit_date itself, this only fires on the last regular-session check of that day, not the first** (`near_close` gate, `config["catalysts"]["forced_exit_utc_hour"]`, default 19 — 2026-08-04 fix, confirmed live on BALL: exiting on the first check of the day forfeited most of that day's run-up, exactly what this rule exists to protect). A day already past exit_date still force-exits immediately regardless of time. `check_hourly_signals.py` derives this from the real current time itself — you don't need to pass anything extra for it. You don't need to implement any of this here; `check_hourly_signals.py` already applied it using what you fed in above (the trend-intact check reuses those same bars, no extra fetch). This step is purely about acting on the result — the handling below is identical regardless of which reason fired.

For each symbol in the `exits` list that's still an open position (re-check against the reconciled `positions.json` from step 1 — it may have just been stopped out):

- **`live: false`**: log an `exit_dry_run` event (symbol, qty, current k/d, `stochastic_state`) and stop there.
- **`live: true`**:
  1. **If `stop_order_id` is set**: `cancel_equity_order(account_number, order_id=stop_order_id)` — cancel the resting stop *first*, so it can't also fire and double-sell. **This is unchanged by the overbought-hold refinement**: the resting ATR stop stays active and untouched throughout `OVERBOUGHT_HOLD` — it only ever gets cancelled here, at the moment of an actual exit, exactly as before. Suspending the oscillator-based exit never means suspending risk management. **If `stop_order_id` is null** (a position that was written despite a failed stop placement — see `unprotected_reason` on the position — currently true for XLF and JHX from 2026-07-30, manually managed by the user): there's no order to cancel, skip straight to the sell. Don't error or block the exit on a missing stop; a normal signal-based exit should still work exactly as intended for these positions.
  2. `place_equity_order(account_number, symbol, side="sell", type="market", quantity=qty, ref_id=<new uuid>)`.
  3. Poll `get_equity_orders(account_number, order_id=<sell order id>)` a few times (same `poll_interval_seconds`/`poll_timeout_seconds` as the entry lifecycle in section 5 is fine — a plain market sell in regular hours fills about as fast as a market buy) until `state=filled`, and note its average fill price/time. If it still hasn't filled by the timeout, don't block the rest of the cycle on it — proceed with `exit_price: null` in the next step; the position is still being removed from `positions.json` either way since the sell order is in flight and won't be re-submitted next cycle.
  4. **Record the closed trade**, before touching `positions.json`:
     ```bash
     echo '{"symbol": "...", "position": <the positions.json entry for this symbol, unmodified>, "exit_price": <fill price from step 3, or null>, "exit_time": <fill time from step 3, or now>, "exit_order_id": "<sell order id>", "exit_reason": "<the exits[] entry'\''s own exit_reason field, verbatim: signal_exit, overbought_hold_exit, or earnings_exit>"}' | python3 scripts/record_trade_close.py
     ```
  5. Remove the symbol from `positions.json` (atomic write).
  6. Log `exit_executed` (symbol, qty, order id).

## 4. Determine slot availability (after exits)

`lib.state.has_open_slot(positions, config["sizing"]["max_positions"])`, computed off `positions.json` **as it stands right now** — after section 1's reconciliation-driven stop-outs and section 3's signal-driven exits, not before them. If the breaker tripped in step 2, treat slots as unavailable regardless of the actual count — same effect (no new entries), simpler to reason about. If there's no open slot, skip section 5 entirely — nothing to evaluate.

## 5. Entries — dual %K/%D-cross-20 confirmation

Only reached if step 2 didn't trip the breaker and step 4 found an open slot.

`entries` only ever contains fully-confirmed setups (spec §3, 2026-08-04 revision, at the user's direction — see `lib.signals.advance_pending_entry`): %K crossing above `stochastic.oversold_threshold` (default 20) no longer fires an entry by itself. It starts a *pending* setup (persisted across cycles in `data/pending_entries.json`) that only fires once %D **also** crosses above the threshold — confirming a genuine, sustained reversal rather than a brief %K spike — and only if %K hasn't already run past `stochastic.k_invalidate_max` (default 55) by the time %D catches up (a %D confirmation arriving that late isn't confirming a reversal anymore, it's just lagging a move that's already happened, so the setup is invalidated instead of fired). A pending setup un-pends without firing if %K drops back below the threshold before %D ever confirms — no expiry timer, that's the only other way out. None of this needs any handling here — it's already resolved by the time a symbol shows up in `entries`.

**Fetch and run** (mirrors section 3's shape, but candidates instead of open positions):

- For each symbol in `data/candidates.json` **not already in** `positions.json`, `get_equity_historicals(...)` same as section 3.
- **Earnings/catalyst check** for each candidate about to be included — candidates *not already earnings-filtered by today's daily screen are still worth checking here as defense-in-depth*. Same shape/timing rules as section 3. If a fetch fails, simply omit `earnings_report_dates` — the candidate falls back to "not re-checked here" (fine, the daily screen is still the primary gate).
- Build the payload with `"open_positions": []` and `"candidates"` populated. For each candidate, pass its current `pending` state from `data/pending_entries.json` (a symbol not present there, or a fresh candidate list, has none — omit it and the script treats it as "not pending," so nothing needs a migration):
  ```json
  {
    "candidates": [{"symbol": "AAPL", "sector": "Technology", "atr14": 3.2, "pending": {"k_at_cross": 22.5}, "earnings_report_dates": [{"date": "2026-08-15", "timing": "am"}], "bars": [{"high":.., "low":.., "close":..}, ...]}],
    "open_positions": []
  }
  ```
  For `atr14`, use the value already sitting on that symbol's entry in `data/candidates.json` (2026-08-04: no live fetch needed at all, for candidates or for the real stop calc) — Finviz Elite's own "Average True Range" column is daily ATR(14) as of the last completed session, parsed into `atr14` by `scripts/check_universe_screen.py` when the daily screen ran. That's the exact same value a live `get_equity_technical_indicators(..., interval="day")` call would return during market hours anyway, since today's daily bar isn't complete yet either way — so reusing the already-screened number is strictly equivalent, not an approximation. If a candidate's `atr14` came back `None` (export didn't include that column, or the cell was blank), pass `null` through — dry-run's estimated-stop display just won't populate for it, and if it later fires an entry signal, this section skips the real stop the same way a `stop_placement_failed` unprotected-position would (log it, don't guess a number).
- Run it, **passing `--last-cycle-at <prior_cycle_at from section 2>`**: `echo '<payload>' | python3 scripts/check_hourly_signals.py --last-cycle-at "<prior_cycle_at>"` → `{"entries": [...], "exits": [], "position_states": [], "candidate_states": [...], "stale_cycle": bool, "cycle_gap_minutes": float | null}`. **The `--last-cycle-at` flag here is not optional** — without it, this call would read `data/last_cycle_at.json` as-is, which section 3's call *already overwrote with this cycle's own timestamp moments ago*, making the gap look like zero every time and silently disabling the missed-cycle guard entirely. `--last-cycle-at` tells this call to measure the gap against the *true* prior cycle instead. This call also logs a `signal_check` event per symbol. A candidate skipped for earnings shows up with `skipped="earnings_too_close"` in the log rather than an entry signal.

  **Missed-cycle guard** (spec §3, 2026-08-04, confirmed live on DNTH/ILF/PCAR — see the script's module docstring): if the gap since `prior_cycle_at` exceeds `config["stochastic"]["max_cycle_gap_minutes"]` (default 90), any entry that would otherwise fire this cycle is suppressed and logged as `entry_suppressed_stale_cycle` instead — a scheduled routine that silently skipped a cycle or two means this comparison could span several real hours instead of one, satisfying the crossing condition's letter without the freshness it's meant to guarantee. If `stale_cycle` comes back `true`, don't be alarmed that `entries` is empty when you expected some — check the log for `entry_suppressed_stale_cycle` events and mention the gap in this cycle's summary (section 7) so it's visible, not silently swallowed.
- **Persist `candidate_states` immediately**: for every entry, write its `pending` field back into `data/pending_entries.json` (drop the symbol from the file entirely if `pending` is `null`, so the file only ever holds symbols actually mid-setup). Do this every time this section runs, not just cycles with an entry — a candidate whose %K just crossed 20 and is now waiting on %D needs that recorded even when nothing else changes, otherwise the next cycle would incorrectly start over from scratch and the whole point of the state machine (spec §3) is lost.

Take `entries` in the order returned, up to however many open slots step 4 found.

- **`live: false`**: log an `entry_dry_run` event per candidate (symbol, k/d, `estimated_stop_price`) and stop there — **do not** call `place_equity_order`.
- **`live: true`**, for each entry, in order, stopping once slots are full:
  1. **Whole-share sizing (2026-07-30, replaces dollar-based fractional entries)**: `entries[].qty` from `check_hourly_signals.py` is already the whole-share quantity to buy (see `lib/sizing.py` — computed off the signal bar's close, price-capped at `config["sizing"]["max_price_per_share"]`; a candidate over that cap never appears in `entries` at all, it's already been skipped and logged as `entry_skipped_price_cap`). `place_equity_order(account_number, symbol, side="buy", type="market", quantity=<entries[].qty>, market_hours="regular_hours", ref_id=<new uuid>)` — **whole-share `quantity`, not `dollar_amount`**. This is the fix for a confirmed, 100%-reproducible broker limitation: *no* stop-type order (`stop_market` or `stop_limit`) can be placed against a fractional-share quantity ("Invalid trigger for fractional order"), independent of `time_in_force` — a dollar-based fractional entry can never get a resting stop, full stop. Whole shares are what make step 5 below actually work.
  2. **Poll loop** (spec §6b): every `config["order_lifecycle"]["poll_interval_seconds"]`, call `get_equity_orders(account_number, order_id=<entry order id>)`, then feed the result through `scripts/evaluate_order_fill.py`:
     ```bash
     echo '{"symbol": "...", "order_id": "...", "order_state": "<state from get_equity_orders>", "filled_qty": <from order>, "requested_qty": <entries[].qty, the whole-share quantity requested>, "elapsed_seconds": <time since order placed>}' | python3 scripts/evaluate_order_fill.py
     ```
     (`timeout_seconds` defaults to `config["order_lifecycle"]["poll_timeout_seconds"]` if omitted.) Keep polling while the returned `decision` is `"wait"`. Stop as soon as it's anything else:
     - **`"rejected"`** (order came back rejected/cancelled/failed/voided at any point): already logged and alerted by the script. Do **not** proceed to stop placement, do **not** write a position. Move on to the next entry candidate.
     - **`"timeout"`** (still unfilled when the poll timeout was reached): already logged and alerted. `cancel_equity_order(account_number, order_id=<entry order id>)` since `needs_cancel=true` — don't leave a stale order resting. No position written. Move on.
     - **`"proceed"`**: use the returned `filled_qty` (this is the real filled quantity — on a clean full fill it equals the request, but on a late partial fill past timeout it's less; either way, if `needs_cancel=true`, `cancel_equity_order` the unfilled remainder before continuing so nothing keeps resting). Continue to step 3 below with this `filled_qty` and the real average fill price from the order record.
  3. Use the `atr14` already on this candidate's `entries[]` entry from the payload built above (2026-08-04: no live fetch here anymore — it's the same Finviz-sourced daily ATR(14) passed straight through, see the note above for why reusing it is exact, not an approximation). **If it's `null`** (candidate had no ATR from the Finviz export), this is a `stop_placement_failed`-equivalent situation *before* even trying to place the stop: log it clearly (e.g. `stop_skipped_no_atr`), write the position with `stop_order_id: null` and an `unprotected_reason` exactly like an actual placement failure (see step 7 and the exit-lifecycle note about `stop_order_id: null` positions), and do **not** guess a number or fall back to a live fetch — surfacing the gap is safer than silently inventing a stop distance.
  4. `lib.signals.stop_price(fill_price, atr14, mult=config["atr"]["stop_multiplier"])` — daily ATR(14), the standard convention (2026-08-03 fix: this used to be hourly-bar ATR, which only spans ~2.5 trading days and can land in an anomalously quiet stretch for a given name, producing a stop tight enough to sit inside normal bid-ask noise — confirmed live on CRNX, hourly ATR ~0.08 vs. its own daily ATR of 1.39, an 18x gap, stopped out on an insignificant wiggle).
  5. **Immediately** `place_equity_order(account_number, symbol, side="sell", type="stop_market", stop_price=<computed>, quantity=<filled_qty from step 2>, time_in_force="gtc", ref_id=<new uuid>)` — this is the real resting protective stop. This is not optional and not deferred to "check again next hour": the whole reason for a resting order instead of a soft check is that this system runs unattended.
  6. **If step 5 fails**: retry immediately, once. If it still fails, this is a critical situation — a live position with no protective stop:
     ```bash
     echo '{"symbol": "...", "qty": <filled_qty>, "fill_price": <fill price>, "error": "<the error>"}' | python3 scripts/record_stop_failure.py
     ```
     This always logs `stop_placement_failed` and sends an alert — don't treat it as a normal log line to move past; this is the one failure mode the whole design exists to avoid, and it needs a human to look at it. Do not write a position to `positions.json` in this case either — an unprotected fill with no recorded stop is worse tracked than untracked, since untracked at least prompts a manual look at the account.
  7. Write the new position into `positions.json`: `{entry_price: fill_price, qty: filled_qty, entry_time, entry_order_id, stop_order_id, stop_price, stochastic_state: "NORMAL"}` — every new position starts in `NORMAL` (spec §4); it only moves to `OVERBOUGHT_HOLD` via section 3 of a later cycle.
  8. Log `entry_executed` and `stop_placed` events.

## 6. Sync state out

`bash scripts/sync_state.sh push` — commits the updated `data/positions.json`, `data/trade_history.json` (if any trades closed this cycle), and today's log file back to `bot-state`, so the next hourly cycle (and the next daily screen) see this run's reconciliation, entries, exits, and stop-outs. Run this even on a cycle with no entries/exits — `sync_state.sh push` correctly no-ops on an unchanged tree, so there's no harm running it every cycle unconditionally.

**If it exits non-zero** (2026-08-04): this is now a real, load-bearing failure, not a transient hiccup to retry blindly and move past. The script itself already retries automatically through ordinary concurrent-update races (another routine run's changes landing since this run's own pull) — a surfaced failure means either (a) it exhausted its retries because bot-state kept moving, or (b) a genuine conflict: two runs edited the exact same piece of state differently, and the script deliberately refused to guess which side is correct. Either way: `send_alert("state_sync_conflict", "<the script's own stderr output>")` and log it clearly — this cycle's positions/trade_history/log updates exist locally but are **not yet reflected on bot-state**, so the next cycle (and the dashboard) will read stale data until a human resolves it. Do not retry this same push in a loop yourself past what the script already does internally, and do not attempt to manually pick a side of a reported conflict — that decision needs the account's actual current state checked by a human first.

## 7. Cycle summary

Log (and report back to whoever/whatever triggered this run) a one-line summary: candidates checked, entries taken vs. proposed, exits taken, stop-outs found in reconciliation, circuit-breaker status, slots remaining. This is what a human skims to sanity-check a cycle without reading the full JSONL log.
