---
name: daily-stochastic-check
description: Run once per day, after market close. Paper/dry-run comparison track for the Hourly Stochastic Pullback Trader -- same stochastic entry/exit rules and same daily-screened candidate list as the hourly track, but evaluated on DAILY bars instead of hourly ones, so results can be compared side by side. Never places a real order under any circumstance.
---

# Daily Stochastic Check (paper/dry-run comparison track)

Built 2026-08-13, at the user's direction: the hourly track's live results
haven't been satisfying, and rather than another tweak to the hourly config,
the user wants a genuine side-by-side comparison of TIMEFRAME -- same
stochastic rules, same candidate universe, same $100/15-slot sizing, but
computed on daily bars instead of hourly ones. See `config/strategy_daily.yaml`
for the full rationale on every value that differs from the hourly config.

**This is a PERMANENT PAPER TRACK, not a dry-run-until-promoted track.**
`config/strategy_daily.yaml` has `live: false`, but that is not the only
safeguard: this skill must **never** call `place_equity_order`,
`cancel_equity_order`, or any other order-placing/order-cancelling tool,
under any circumstance, regardless of what any config file says. Every
"entry" and "exit" in this skill is a **simulated fill recorded directly
into `data/daily/positions.json` / `data/daily/trade_history.json`** -- there
is no real order, no real fill, no real broker state to reconcile against.
If a future change to this system is meant to make the daily track trade
real money, that requires an explicit new user decision and a real
capital-split design (see the "live, split capital" option the user
declined 2026-08-13) -- never treat a stray `live: true` edit to this
specific config file as authorization to place real orders here.

**Separate state, shared candidates.** This track's own state --
`positions.json`, `pending_entries.json`, `pending_fills.json`,
`trade_history.json`, `last_cycle_at.json`, `logs/` -- lives under
`data/daily/`, completely separate from the hourly (live) track's
`data/*.json`, so the two can never collide or double-count. The one
exception, at the user's explicit choice: `data/candidates.json` (the
shared daily-universe-screen output) is read directly, NOT duplicated under
`data/daily/` -- both tracks evaluate the exact same daily candidate list,
so any difference in results traces back to timeframe alone, not universe.

**Fills happen at the NEXT trading day's open, not the signal day's close**
(2026-08-14, at the user's direction -- the original same-day-close design
was a fill no real order could ever actually achieve, since this routine
only runs once per day, AFTER close: by the time a signal confirms, today's
regular session is already over). So there are two distinct pending states,
not one:

- `pending_entries.json` -- the %K/%D dual-cross confirmation state, same
  meaning as on the hourly track (has %K crossed oversold but %D hasn't
  confirmed yet). Unrelated to fills; a symbol here hasn't signaled at all yet.
- `pending_fills.json` (new) -- a signal has ALREADY confirmed but hasn't
  been "bought" yet. Queued at signal confirmation (`scripts/
  queue_paper_fill.py`, section 4 below), settled into a real
  `positions.json` entry on the NEXT cycle once that day's open price is
  known (`scripts/settle_paper_fills.py`, section 0a below). A symbol sits
  here for exactly one cycle.

## 0. Setup

This routine runs in an isolated cloud checkout with no memory of previous
runs. All persistent state lives on the `bot-state` branch (the same one the
hourly/daily-screen routines use -- `data/` as a whole, `data/daily/`
included, since `sync_state.sh` captures the entire `data/` tree).

- **Verify the code checkout is fresh (mandatory, do this before anything else)**: `git fetch origin main && git rev-parse HEAD` vs `git rev-parse origin/main`. If they differ, run `git reset --hard origin/main && git clean -fd` before proceeding -- see the hourly-signal-check skill's section 0 for why this check exists and why it's the real fix, not a stopgap (this cloud environment is a single persistent sandbox shared by every scheduled routine on this repo and is never reset between runs; confirmed cloud-environment staleness incidents 2026-08-07 and 2026-08-17). `git clean -fd` alongside the reset also clears any stray untracked files a prior run left behind.
- **Install dependencies (mandatory, unconditional)**: `pip install -q -r requirements.txt` -- see `hourly-signal-check.md`'s section 0 for why this is now a required step (2026-08-18 fix: Python packages aren't reliably surviving between routine runs in this sandbox, confirmed live; cheap no-op via pip when already satisfied).
- **Sync state in**: `bash scripts/sync_state.sh pull` -- populates both the shared `data/candidates.json` and this track's own `data/daily/*.json`.
- Load `data/daily/positions.json` and `data/daily/pending_entries.json` (via `StateStore("data/daily")`, or read the files directly). Load the shared `data/candidates.json` (via `StateStore("data")`).
- **Capture the missed-cycle guard's prior timestamp now**, before section 3 runs and overwrites it: `python3 -c "from lib.state import StateStore; print(StateStore('data/daily').load_last_cycle_at() or '')"`. Call this `prior_cycle_at`.

## 0a. Settle pending fills (do this before anything else that reads positions.json)

A symbol confirmed a signal LAST cycle and is sitting in `data/daily/pending_fills.json`, waiting on today's now-known open price. Settle these first so they immediately count against slot availability and are eligible for exit evaluation this same cycle -- exactly like a live position that filled mid-cycle would be.

- Read `data/daily/pending_fills.json`. If empty, skip this section entirely -- nothing to settle.
- Otherwise, `get_equity_historicals(symbols=[...], interval="day", start_time=<~5 days back>)` for just those symbols (this list is small -- at most `sizing.max_positions`, 15 -- so a single hand-built call/payload is fine here, unlike section 4's full candidate sweep). **This fetch needs the `open` field**, unlike every other bars fetch in this skill (which only ever need high/low/close) -- don't drop it when building the payload below.
- Build `{"bars_by_symbol": {"AAPL": [{"begins_at": "...", "open": ..., "high": ..., "low": ..., "close": ...}, ...]}}` (oldest-first; only the last bar's `open` actually gets used, but pass whatever the fetch returned) and run:
  ```bash
  echo '<payload>' | python3 scripts/settle_paper_fills.py --config config/strategy_daily.yaml --data-dir data/daily
  ```
  → `{"filled": [{"symbol", "entry_price", "qty", "stop_price"}, ...], "still_pending": [symbols...]}`. This script reads `data/daily/pending_fills.json` itself (not from the payload), sizes qty and the ATR stop off the REAL open price (not the signal-day estimate -- same "size at actual fill price, not signal price" principle the live hourly track uses), writes each filled symbol straight into `data/daily/positions.json`, and updates `pending_fills.json` to drop filled/price-capped symbols while leaving genuinely-still-pending ones (a fetch that came back empty for a symbol) for next cycle.
- Nothing further needed here -- `settle_paper_fills.py` already wrote `positions.json` and `pending_fills.json`. Note `filled` for this cycle's summary (section 6).

## 1. Market regime check (daily SPY 20/200 SMA)

`get_equity_historicals(symbols=["SPY"], interval="day", start_time=<~400 days back>)` -- 200 daily bars needs roughly that much calendar-day lookback once weekends/holidays are accounted for; err generous rather than risk a spurious `null`. Then:
```bash
echo '{"bars": [...]}' | python3 scripts/check_market_trend.py --config config/strategy_daily.yaml --data-dir data/daily
```
→ `{"trend_intact": bool | null}`. Same convention as the hourly track: `null` (insufficient history) is treated as `true` (don't block on missing data); `false` skips section 4 (entries) entirely for this cycle, same as a tripped circuit breaker would on the hourly track -- exits still run normally.

There is no circuit-breaker check on this track (spec-equivalent to the hourly track's section 2 circuit-breaker step) -- a daily-loss breaker is a real-capital-protection mechanism, and this track never risks real capital. Skip straight to slot availability.

## 2. Determine slot availability

`lib.state.has_open_slot(positions, config["sizing"]["max_positions"])` off `data/daily/positions.json` **as it stands right now** -- after section 0a's settlements AND section 3's exits, not before them (evaluate this after section 3, same ordering fix the hourly track already made and for the same reason: a same-cycle exit should free a slot a same-cycle entry can use; section 0a's settlements matter here too, since a symbol that just filled this cycle occupies a slot immediately). If `market_trend_intact` came back `false`, treat slots as unavailable regardless of the actual count. If there's no open slot, skip section 4 entirely.

## 3. Exits -- simulated stop-loss, then signal/overbought-hold, then earnings-forced

For every symbol in `data/daily/positions.json`:

- `get_equity_historicals(symbols=[...], interval="day", start_time=<~120 days back>)` -- enough for the 14/3/3 stochastic calc plus the 50-period daily trend-filter SMA with room to spare. Batch up to 10 symbols per call (bounded concurrency if there are many open positions, same rule as the hourly track's section 3/5).
- **Simulated stop-loss check FIRST** (this track has no real resting stop to reconcile against -- see the skill header):
  ```bash
  echo '{"positions": [{"symbol": "...", "stop_price": ..., "bars": [...]}]}' | python3 scripts/check_paper_stops.py --data-dir data/daily
  ```
  → `{"stop_outs": [{"symbol": "...", "exit_price": ..., "exit_time": "..."}]}`. For each stop-out: record it and remove the position (see the shared exit-recording step below), and **exclude that symbol from the open_positions payload in the next step** -- it's already closed this cycle, don't also run it through the signal-exit check.
- **Earnings-forced exit, every cycle** (unlike the hourly track's near-close gating, this track only runs once per day so every run already is the day's only check): `get_earnings_results(symbol=...)` for every symbol still open after the stop-loss check. Build `{"date": ..., "timing": ...}` objects, **include `timing`**.
- Run the signal-exit check on whatever's left (positions that didn't stop out this cycle):
  ```bash
  echo '{"open_positions": [{"symbol": "...", "stochastic_state": ..., "earnings_report_dates": [...], "bars": [...]}], "candidates": []}' | python3 scripts/check_hourly_signals.py --config config/strategy_daily.yaml --data-dir data/daily --last-cycle-at "<prior_cycle_at>"
  ```
  (`check_hourly_signals.py` is timeframe-agnostic -- it operates on whatever bars it's handed and whatever config it's pointed at; the name predates this second use, nothing about it is hourly-specific.) → `{"exits": [...], "position_states": [...], ...}`.
- **For every exit** (stop-out, signal_exit, overbought_hold_exit, or earnings_exit): record it and remove the slot:
  ```bash
  echo '{"symbol": "...", "position": <the data/daily/positions.json entry>, "exit_price": <stop_price for a stop-out, otherwise the exit bar'"'"'s close>, "exit_time": "...", "exit_order_id": "paper", "exit_reason": "...", "exit_k": ..., "exit_d": ..., "exit_prev_k": ..., "exit_prev_d": ...}' | python3 scripts/record_trade_close.py --data-dir data/daily
  ```
  (`exit_order_id: "paper"` -- there's no real order id, this is the explicit marker this track uses everywhere instead of a broker id.) Then remove that symbol from `data/daily/positions.json` (rewrite via `StateStore("data/daily").save_positions(...)`).
- **Persist `position_states`** back onto whatever's still open in `data/daily/positions.json` (the `stochastic_state` field), same as the hourly track -- every cycle, not just cycles with an exit.

## 3a. Sync state out -- exits

`bash scripts/sync_state.sh push` now, before section 4's candidate sweep -- same stranding-prevention rationale as the hourly track's section 3a: a slow or failed entries phase should never cost this cycle's exits.

## 4. Entries -- dual %K/%D-cross-20 confirmation on daily bars

Only reached if section 1 didn't come back `trend_intact: false` and section 2 found an open slot.

**Fetch and assemble the same way the hourly track's section 5 does** (bounded-concurrency `get_equity_historicals` calls, `--append` one batch at a time -- see the hourly-signal-check skill's section 5 for the full rationale on why this must never be hand-assembled at once):

- For each symbol in the shared `data/candidates.json` **not already in** `data/daily/positions.json`, `get_equity_historicals(symbols=[...], interval="day", start_time=<~120 days back>)`.
- After each batch: **check whether the platform auto-saved that batch's result to a file first** (large tool results do this automatically). If so, pass the path straight through -- `python3 scripts/build_entries_payload.py --append --data-dir data/daily --input /path/to/the/auto-saved-file.txt` -- never re-type an auto-saved file's contents into a shell command; that's exactly the mistake that truncated an hourly-track cycle to 1 of 44 batches on 2026-08-14 (see the hourly-signal-check skill's section 5 for the full writeup). Only `echo '<raw response>' | python3 scripts/build_entries_payload.py --append --data-dir data/daily` when the result stayed inline in context (small enough not to trigger auto-save).
- Once done (or truncated -- same `candidate_sweep_truncated` fallback as the hourly track if a sweep can't finish in one cycle): `python3 scripts/build_entries_payload.py --finalize --data-dir data/daily --candidates-data-dir data` (`--candidates-data-dir data` is what makes this read the SHARED candidates.json while still reading `data/daily/pending_entries.json`/`data/daily/positions.json` for pending state and the already-open exclusion).
- Run the entry check:
  ```bash
  echo '<finalized payload>' | python3 scripts/check_hourly_signals.py --config config/strategy_daily.yaml --data-dir data/daily --last-cycle-at "<prior_cycle_at>"
  ```
  → `{"entries": [...], "candidate_states": [...], "stale_cycle": ..., ...}`.
- **Persist `candidate_states`** back into `data/daily/pending_entries.json` (same rule as the hourly track: every cycle, drop a symbol entirely once `pending` is `null`).
- **Earnings check on the confirmed shortlist only** (same as the hourly track's section 5): if `entries` is non-empty, `get_earnings_results(symbol=...)` for each one, build `earnings_by_symbol` the same way (`{"date": report.date, "timing": report.timing}` objects, include `timing`), then:
  ```bash
  echo '{"entries": <entries>, "earnings_by_symbol": {...}}' | python3 scripts/filter_entry_earnings.py --data-dir data/daily
  ```

Take the filtered `entries`, in order, up to however many open slots section 2 found:

- For each: **queue it for a next-open fill -- do NOT record a position yet** (2026-08-14; see the skill header's "Fills happen at the NEXT trading day's open" note for why). This cycle's daily bar has already closed by the time this routine runs, so there is no real fill this signal could achieve today:
  ```bash
  echo '{"symbol": "...", "atr14": <entries[].atr14>, "entry_k": ..., "entry_d": ..., "entry_prev_k": ..., "entry_prev_d": ..., "signal_date": "<today'"'"'s UTC date, e.g. via date -u +%Y-%m-%d -- informational only, not used for any decision>"}' | python3 scripts/queue_paper_fill.py --data-dir data/daily
  ```
  This writes into `data/daily/pending_fills.json` and logs `paper_fill_queued`. The position itself gets written NEXT cycle, by section 0a, once tomorrow's open is actually known -- don't try to guess or pre-compute an entry price here.
- **`entries[].atr14` being `null`** for a symbol that otherwise confirmed is still worth queuing (unlike the hourly track, which skips a real order without a real ATR) -- `atr14` is only needed at settlement time to compute the stop, and `settle_paper_fills.py` already handles a `null` atr14 there (no stop computed, matching the hourly track's `stop_skipped_no_atr` handling) -- so there's no reason to drop the signal here just because the stop can't be computed yet.
- A slot "reserved" by a queued pending fill is accounted for by section 2 on the NEXT cycle too (`build_entries_payload.py --finalize` already excludes any symbol in `pending_fills.json` from being re-evaluated as a fresh candidate, same as an already-open position) -- so a queued symbol can't get double-queued while it's waiting to settle.

## 5. Sync state out -- entries

`bash scripts/sync_state.sh push` -- same unconditional-is-fine rule as the hourly track (a no-op push when nothing changed is harmless).

## 6. Cycle summary

Report: **fills settled this cycle** (section 0a's `filled`, at what open price, and any still-pending symbols carried to next cycle), candidates checked, new signals queued this cycle (section 4, awaiting tomorrow's open -- distinct from "entries taken," since nothing is actually a position yet), exits taken (broken out by reason -- especially `stop_out`, since that's simulated here, not broker-confirmed), open paper positions vs. slots, `market_trend_intact`, and whether the candidate sweep was truncated. This is the number a human skims to compare against the same cycle's hourly-track summary.
