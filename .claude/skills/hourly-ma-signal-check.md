---
name: hourly-ma-signal-check
description: Run hourly during market hours (staggered a few minutes from hourly-signal-check so the two routines don't collide on the shared cloud sandbox). Reconciles the MA Pullback / Breakout-Retest agent's open positions, checks its own circuit breaker and the shared SPY market-regime filter, evaluates exits FIRST (daily-bar-driven: partial profit at 1.5R, post-partial chandelier trail, trend-invalidation with a 3-day grace period, time stop, hard stop) and syncs immediately, then evaluates the reclaim-entry trigger for eligible watchlist symbols (hourly bars), filters against fresh earnings, and (only if config live=true) places real orders with an immediate resting broker-side stop.
---

# Hourly MA Signal Check

Companion routine to `hourly-signal-check`, for the MA Pullback /
Breakout-Retest agent — a separate, independent trading system in this same
repo with its OWN capital sleeve, config (`config/ma_pullback_strategy.yaml`),
and state directory (`data/ma_pullback/`). Plan:
`/Users/dustinrowley/.claude/plans/tidy-knitting-castle.md`.

**Read `config/ma_pullback_strategy.yaml` first.** Every threshold and the
`live` flag live there — this doc describes the procedure, not the numbers.
If `live: false` (the default), this run computes and logs everything but
**must not call any order-placing or order-cancelling tool**. Treat that as
a hard rule, not a suggestion — exactly the same convention as
`hourly-signal-check.md`.

**Two hard rules, called out here in prose because getting either wrong was
the single most costly bug in the reference implementation's development
history:**
1. **Never enter below `breakout_level`.** The reclaim trigger
   (`lib.ma_signals.evaluate_entry`) already implies `close >=
   breakout_level`, but this is checked again, explicitly, as its own
   never-relaxed condition — if a future change to the reclaim logic ever
   weakens that implication, this second check is what still stops an
   entry from firing below the level. Don't skip validating this if you're
   ever looking at raw entry data by hand.
2. **Every filled entry gets an immediate resting broker-side `stop_market`
   order, not just the once-per-daily-bar `evaluate_exit` check.** The
   daily-close exit check is the strategy logic; the broker-side stop is
   what actually protects the position intra-day between checks. This is
   not optional and not deferred to "check again tomorrow."

## 0. Setup

Same shared, never-reset cloud sandbox as every other routine in this repo
— see `hourly-signal-check.md`'s section 0 for the confirmed-live staleness
incidents that established this pattern; it applies identically here.

- **Verify the code checkout is fresh (mandatory, first)**: `git fetch
  origin main && git rev-parse HEAD` vs `git rev-parse origin/main`; if they
  differ, `git reset --hard origin/main && git clean -fd`. Log
  `stale_checkout_detected` if corrected.
- **Sync state in (mandatory, first)**: `bash scripts/sync_state.sh pull` —
  populates `data/`, including `data/ma_pullback/{watchlist,positions,
  trade_history,last_cycle_at}.json`. A missing `bot-state` branch or a
  missing `data/ma_pullback/` on it is fine — everything reads as
  empty/missing, the correct starting state for a fresh setup.
- Load `data/ma_pullback/positions.json` and `data/ma_pullback/
  watchlist.json` (read directly, or via `StateStore('data/ma_pullback')`).
  `data/ma_pullback/last_cycle_at.json` is read/written directly by
  `check_ma_hourly_signals.py` itself — nothing to do with it here beyond
  the normal sync in/out.

## 1. Reconcile first

Same pattern as `hourly-signal-check.md` section 1, against this agent's own
positions: for each symbol in `data/ma_pullback/positions.json` with a
`stop_order_id`, `get_equity_orders(account_number, order_id=stop_order_id)`.
If `state=filled`, it was stopped out at the broker since the last cycle:

```bash
echo '{"symbol": "...", "position": <the positions.json entry, unmodified>, "exit_price": <avg fill price>, "exit_time": <fill time>, "exit_order_id": "<stop_order_id>", "exit_reason": "stop_out"}' | python3 scripts/record_trade_close.py --data-dir data/ma_pullback
```

Then clear that symbol from `data/ma_pullback/positions.json` (atomic
write) and log a `stop_out` event. Cross-check against
`get_equity_positions(account_number)` for drift, same as the stochastic
skill — log `reconciliation_drift` on a mismatch, don't guess a fix.

**Account number**: `config/ma_pullback_strategy.yaml`'s own `account_number`
field — the SAME account as the stochastic system (confirmed with the user,
2026-08-17), not a separate sub-account. Separation between the two
systems' capital is enforced by this agent's own `sizing.max_positions`
(10 slots) and `risk.max_daily_loss_pct` circuit breaker, not by account
isolation — a bad cycle on one system can't drain the other's sleeve, but
both draw against the same buying power. The user is adding $1,500 to the
account specifically to cover this agent's max theoretical exposure
(10 slots × ~$150/slot).

## 2. Circuit breaker + market-regime check

- `get_portfolio(account_number)` → account value, unrealized P&L (this
  agent's sleeve only).
- `get_realized_pnl(account_number, span="day", asset_classes=["equity"])`
  → today's realized P&L.
- ```bash
  echo '{"account_value": ..., "realized_pnl_today": ..., "unrealized_pnl_today": ...}' | python3 scripts/check_circuit_breaker.py --config config/ma_pullback_strategy.yaml --data-dir data/ma_pullback
  ```
  → `{"tripped": bool}`. This is the SAME generic `lib.risk.circuit_breaker_tripped`
  the stochastic system uses, just pointed at this agent's own config and
  data directory — its own `risk.max_daily_loss_pct`, so a bad day on one
  system can't drain the other's sleeve. Already logs/alerts on trip, don't
  duplicate. If tripped: **skip section 5 (entries) entirely** this cycle —
  exits still run normally.
- **Market-regime check (shared signal, not duplicated config)**:
  `get_equity_historicals(symbols=["SPY"], interval="minute", start_time=<~2
  days back>)`, then
  ```bash
  echo '{"bars": [...]}' | python3 scripts/check_market_trend.py --config config/strategy.yaml --data-dir data/ma_pullback
  ```
  **Note `--config config/strategy.yaml`, the STOCHASTIC system's own
  config, not this agent's** — this is deliberate: the plan calls this out
  as the spec's "shared market-regime check module ... so both agents read
  the same risk-off signal" (§7). One signal, one place it's defined, no
  duplicated `market_filter` section in `config/ma_pullback_strategy.yaml`.
  `--data-dir data/ma_pullback` still points logging at this agent's own
  logs. `null` (insufficient history) reads as `true` (don't block). If
  `trend_intact` is `false`: **skip section 5 (entries) entirely** this
  cycle, exactly like a tripped breaker.
- **Capture the missed-cycle guard's prior timestamp now**, before section 3
  runs (same reason as `hourly-signal-check.md`'s section 2 — the exits call
  below overwrites `data/ma_pullback/last_cycle_at.json` with this cycle's
  own timestamp): `python3 -c "from lib.state import StateStore;
  print(StateStore('data/ma_pullback').load_last_cycle_at() or '')"`. Call
  this `prior_cycle_at`.

## 3. Exits first — partial profit, chandelier trail, trend invalidation, time stop, hard stop

**Exits run before slot availability is checked**, same reasoning as
`hourly-signal-check.md`'s section 3 — a position closed this cycle frees a
slot a fresh entry signal should be able to use in the same cycle.

This step only needs `open_positions`' DAILY bars (spec: exit logic is
daily-close-driven, matching the validated backtest — see
`lib.ma_signals.evaluate_exit`):

- For every symbol in `data/ma_pullback/positions.json`,
  `get_equity_historicals(symbols=[...], interval="day", start_time=<enough
  lookback for ATR(14) + sma_fast(50) warmup — several months back is
  plenty>, adjustment_type="split")`. Batch up to 10 symbols per call.
- Build the payload:
  ```json
  {
    "open_positions": [
      {"symbol": "MSFT", "entry_price": 101.20, "stop_price": 98.50,
       "target_1": 105.25, "highest_close": 102.10, "bars_held": 3,
       "partial_taken": false, "atr_entry": 1.80, "shares": 37,
       "daily_bars": {"date": [...], "high": [...], "low": [...], "close": [...]}}
    ],
    "watchlist": {}
  }
  ```
  (`"watchlist": {}` — nothing to evaluate for entry yet, that's section 5.)
- Run it:
  ```bash
  echo '<payload>' | python3 scripts/check_ma_hourly_signals.py --config config/ma_pullback_strategy.yaml --data-dir data/ma_pullback
  ```
  → `{"entries": [], "exits": [...], "position_updates": [...], "stale_cycle": false, "cycle_gap_minutes": ...}`.
  This call is also what advances the missed-cycle guard's bookkeeping for
  the whole cycle (writes `data/ma_pullback/last_cycle_at.json` = now) —
  runs unconditionally, same as the stochastic system's equivalent call.
- **Persist `position_updates` immediately**, before doing anything else:
  for every entry, write its `position` dict back onto the matching symbol
  in `data/ma_pullback/positions.json` (atomic write) — **every cycle, not
  just cycles with an exit**. `bars_held`/`highest_close`/a ratcheted-up
  chandelier `stop_price` can all advance without a full exit; skipping this
  on a quiet cycle resets that state machine's progress, same failure mode
  the stochastic system's `position_states` persistence step exists to
  avoid.

For each entry in `exits` (action is `"partial"`, `"trend_invalidated"`,
`"time_stop"`, or `"stop_hit"`) that's still an open position (re-check
against the reconciled `positions.json` from step 1):

- **`live: false`**: log an `ma_exit_dry_run` event (symbol, action,
  updated stop/target fields) and stop there.
- **`live: true`**:
  - **`action == "partial"`**: sell `config["stop_target"]["partial_profit_pct"]`
    of the CURRENT `shares` (round down, at least 1 share — e.g. `max(1,
    int(shares * partial_profit_pct))`), `place_equity_order(account_number,
    symbol, side="sell", type="market", quantity=<partial qty>,
    ref_id=<uuid>)`. Poll to a fill (same pattern as step 5's poll loop
    below). Then **replace the resting stop** at the new (breakeven or
    ratcheted) `stop_price` from the exit result's `position`: cancel the
    existing `stop_order_id` first if set, then place a fresh
    `stop_market` for the REMAINING share count (`shares - partial_qty`).
    Update `positions.json`: reduce `shares`, set the new `stop_order_id`,
    and write through every other field from the exit result's `position`
    (this is not a full exit — the position stays open, just resized and
    re-stopped). Log `ma_partial_taken`.
  - **`action` is a full exit (`"trend_invalidated"`, `"time_stop"`,
    `"stop_hit"`)**:
    1. If `stop_order_id` is set, `cancel_equity_order(account_number,
       order_id=stop_order_id)` first (avoid a double-sell if it fires
       concurrently). If null, skip straight to the sell (same
       `unprotected_reason`-carrying convention as the stochastic system for
       a position whose stop placement failed at entry).
    2. `place_equity_order(account_number, symbol, side="sell", type="market",
       quantity=<current shares>, ref_id=<uuid>)`.
    3. Poll to a fill (or timeout — proceed with `exit_price: null` if it
       hasn't filled by then, same as the stochastic skill's equivalent
       step; the position is still removed from `positions.json` either
       way).
    4. **Record the closed trade** before touching `positions.json`:
       ```bash
       echo '{"symbol": "...", "position": <the positions.json entry for this symbol, unmodified>, "exit_price": <fill price or null>, "exit_time": <fill time or now>, "exit_order_id": "<sell order id>", "exit_reason": "<action verbatim: trend_invalidated, time_stop, or stop_hit>"}' | python3 scripts/record_trade_close.py --data-dir data/ma_pullback
       ```
       Note: `lib.state.close_trade_record` is reused verbatim — its
       `entry_k`/`entry_d`/`exit_k`/`exit_d` fields are stochastic-specific
       and simply stay `null` here, no migration needed (same convention
       already used elsewhere in this repo).
    5. Remove the symbol from `data/ma_pullback/positions.json` (atomic
       write).
    6. Log `ma_exit_executed` (symbol, action, qty, order id).

## 3a. Sync state out — exits (before touching the watchlist)

`bash scripts/sync_state.sh push` — **do this now, even if `exits` was
empty and reconciliation found nothing**, before section 5's (potentially
slower, more failure-prone) watchlist/entry evaluation even starts. Identical
reasoning to `hourly-signal-check.md`'s section 3a: this is what makes a
live exit (or a `position_updates` state advance) impossible to strand off
`bot-state` if something later in this cycle stalls or errors.

## 4. Determine slot availability

`lib.state.has_open_slot(positions, config["sizing"]["max_positions"])` —
10 slots to start (2026-08-17, at the user's direction, "while we work out
the bugs"; revisit once this agent has a live track record, same convention
as the stochastic system's own `sizing.max_positions` history). If the
breaker tripped or `trend_intact` came back `false` in step 2, treat slots
as unavailable regardless of the actual count, same as the stochastic
skill.

## 5. Entries — reclaim trigger, eligible watchlist symbols only

Only reached if step 2 didn't trip the breaker/regime filter and step 4
found capacity.

For every symbol in `data/ma_pullback/watchlist.json` with
`eligible_for_entry: true` (set by the daily-ma-scan skill's
`check_ma_daily_scan.py` run) that ISN'T already in `positions.json`:

- **Daily bars**: same fetch as section 3, `interval="day"` — reuse
  section 3's fetch if the same symbols appear in both places, otherwise a
  fresh batched fetch, same bounded-concurrency approach
  `hourly-signal-check.md`'s section 5 documents (parallel tool calls
  within one turn, capped concurrency, watch for rate limits).
- **Hourly bars**: `get_equity_historicals(symbols=[...], interval="hour",
  start_time=<enough lookback for volume_confirm_lookback bars — a few
  trading days back is plenty>)`. This is what the reclaim trigger and
  volume-confirmation check are computed off — the daily bars alone can't
  answer "did the just-closed hourly bar reclaim `breakout_level`."
- Build the payload:
  ```json
  {
    "open_positions": [],
    "watchlist": {
      "AAPL": {"breakout_level": 97.32, "retest_low": 94.10, "retest_seen": true,
               "failed": false, "eligible_for_entry": true,
               "daily_bars": {...}, "hourly_bars": {...}}
    }
  }
  ```
  (`"open_positions": []` — already handled in section 3; passing them
  again here would just re-evaluate exits for no reason.)
- Run it, **passing `--last-cycle-at <prior_cycle_at from section 2>`**
  (same reason as the stochastic system: section 3's call already
  overwrote `last_cycle_at.json` with this cycle's own timestamp):
  ```bash
  echo '<payload>' | python3 scripts/check_ma_hourly_signals.py --config config/ma_pullback_strategy.yaml --data-dir data/ma_pullback --last-cycle-at "<prior_cycle_at>"
  ```
  → `{"entries": [...], "exits": [], "position_updates": [], "stale_cycle": bool, "cycle_gap_minutes": ...}`.
  If `stale_cycle` is `true`, don't be alarmed by an empty `entries` —
  check the log for `ma_entry_suppressed_stale_cycle` events and mention the
  gap in this cycle's summary (section 6).

**Earnings check on the confirmed shortlist only** (reused verbatim, same
pattern as `hourly-signal-check.md`'s equivalent step — `lib.catalysts` +
`scripts/filter_entry_earnings.py`, no MA-specific earnings code): if
`entries` is non-empty, `get_earnings_results(symbol=...)` for each one,
build `earnings_by_symbol` the same way (`{"date": ..., "timing": ...}`
objects, include `timing`), then:
```bash
echo '{"entries": <entries from above>, "earnings_by_symbol": {...}}' | python3 scripts/filter_entry_earnings.py --data-dir data/ma_pullback
```
Use the filtered list for everything below. **Fails closed** — a symbol
omitted from `earnings_by_symbol` is excluded, not passed through, same as
the stochastic system's identical script.

For each entry in the earnings-filtered list, in order, stopping once
capacity (section 4) is used up:

- **`live: false`**: log an `ma_entry_dry_run` event (symbol, entry_price,
  stop_price, target_1, shares, breakout_level) and stop there — **do not**
  call `place_equity_order`.
- **`live: true`**:
  1. **Hard rule check, restated**: verify `entry_price >= breakout_level`
     on this entry before doing anything else — this should always already
     be true (`lib.ma_signals.evaluate_entry` enforces it), but this is the
     one thing worth a second look before a real order goes out, per this
     skill's opening section.
  2. `place_equity_order(account_number, symbol, side="buy", type="market",
     quantity=<entries[].shares>, market_hours="regular_hours",
     ref_id=<uuid>)` — whole-share `quantity`, already sized by
     `lib.sizing.entry_share_quantity` (same logic as the stochastic
     system: ~`sizing.per_trade_usd` notional, floored to whole shares; a
     candidate priced over `sizing.max_price_per_share` never reaches
     `entries[]` at all — already excluded by `lib.ma_signals.evaluate_entry`).
  3. **Poll loop**, identical mechanics to `hourly-signal-check.md`'s
     section 5 step 2:
     ```bash
     echo '{"symbol": "...", "order_id": "...", "order_state": "...", "filled_qty": ..., "requested_qty": <entries[].shares>, "elapsed_seconds": ...}' | python3 scripts/evaluate_order_fill.py --config config/ma_pullback_strategy.yaml --data-dir data/ma_pullback
     ```
     `"rejected"`/`"timeout"` → no position written, move to the next
     candidate (already logged/alerted). `"proceed"` → continue with the
     real `filled_qty` and average fill price.
  4. **Immediately place the resting stop** — this is hard rule #2 from
     this skill's opening section, not optional:
     `place_equity_order(account_number, symbol, side="sell",
     type="stop_market", stop_price=<entries[].stop_price>,
     quantity=<filled_qty>, time_in_force="gtc", ref_id=<uuid>)`.
  5. **If step 4 fails**: retry once. If it still fails:
     ```bash
     echo '{"symbol": "...", "qty": <filled_qty>, "fill_price": <fill price>, "error": "<the error>"}' | python3 scripts/record_stop_failure.py --data-dir data/ma_pullback
     ```
     Always logs `stop_placement_failed` and alerts — a live, unprotected
     position needs a human to look at it immediately. Do not write a
     position to `positions.json` in this case.
  6. Write the new position into `data/ma_pullback/positions.json`:
     `{entry_price: fill_price, stop_price: entries[].stop_price, target_1:
     entries[].target_1, highest_close: fill_price, bars_held: 0,
     partial_taken: false, atr_entry: entries[].atr_entry, shares:
     filled_qty, breakout_level: entries[].breakout_level, entry_time,
     entry_order_id, stop_order_id}`.
  7. Log `ma_entry_executed` and `ma_stop_placed` events.

## 6. Sync state out — entries

Second push of the cycle, same reasoning as `hourly-signal-check.md`'s
section 6: `bash scripts/sync_state.sh push`, run unconditionally even on a
no-entries cycle. **If it exits non-zero**: `send_alert("state_sync_conflict",
"<the script's own stderr>")`, log clearly, don't retry in a loop, don't
guess a resolution — identical handling to the stochastic skill.

## 7. Cycle summary

Log (and report back) a one-line summary: watchlist symbols evaluated,
entries taken vs. proposed, exits/partials taken, stop-outs found in
reconciliation, circuit-breaker/market-regime status, and any
stale-cycle/earnings-exclusion notes — same purpose as
`hourly-signal-check.md`'s section 7, so a human can skim-verify a cycle
without reading the full JSONL log.
