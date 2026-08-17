---
name: daily-ma-scan
description: Run once per day (before market open, staggered a few minutes from daily-universe-screen so the two routines don't collide on the shared cloud sandbox). Builds/updates the MA Pullback / Breakout-Retest agent's watchlist (data/ma_pullback/watchlist.json) from a manually-exported Finviz Elite CSV plus daily price history. Never places trades.
---

# Daily MA Scan

Companion routine to `daily-universe-screen`, for the MA Pullback /
Breakout-Retest agent (a separate, independent trading system in this same
repo — see README's "MA Pullback / Breakout-Retest Agent" section). Plan:
`/Users/dustinrowley/.claude/plans/tidy-knitting-castle.md`.

Purpose: refresh `data/ma_pullback/watchlist.json` — the per-symbol
breakout/retest tracking state the hourly-ma-signal-check skill scans for
eligible entries. Screening itself is Finviz-backed and fully manual, exactly
like the stochastic system's daily screen, but a **separate export file**
(`data/ma_pullback/finviz_export.csv`, published via `scripts/
publish_ma_finviz_export.sh`, never the stochastic system's own
`data/finviz_export.csv`).

**This skill never places trades, full stop.** Not "rarely" or "only if
`live: true`" — this routine has no branch anywhere that calls an
order-placing tool. It only computes and persists watchlist state. Entries
and exits both happen exclusively in `hourly-ma-signal-check`.

## 0. Setup

This routine runs in an isolated cloud checkout with no memory of previous
runs and no access to the user's Mac — same shared, never-reset sandbox as
every other routine in this repo (see `daily-universe-screen.md` and
`hourly-signal-check.md` for the confirmed-live incidents that established
this). All persistent state for this agent (`data/ma_pullback/` —
`watchlist.json`, `positions.json`, `trade_history.json`, `finviz_export.csv`,
`last_cycle_at.json`, `logs/`) lives on the same `bot-state` branch the
stochastic system uses, just under its own subdirectory — `scripts/
sync_state.sh` already does `git add -f data/` generically over the whole
tree, so no changes were needed to that script for this agent's state to ride
along on the same push/pull.

1. **Verify the code checkout is fresh (mandatory, do this before anything
   else)**: `git fetch origin main && git rev-parse HEAD` vs `git rev-parse
   origin/main`. If they differ, `git reset --hard origin/main && git clean
   -fd`. This is the same persistent-sandbox staleness issue documented in
   `daily-universe-screen.md`/`hourly-signal-check.md` — it applies equally
   here since this routine shares the same cloud environment. Log a
   `stale_checkout_detected` event if a mismatch was found and corrected.
2. **Sync state in (mandatory, do this first)**: `bash scripts/
   sync_state.sh pull` — populates `data/` (including `data/ma_pullback/*`)
   from `bot-state`. A genuinely fresh setup with no `bot-state` branch yet
   (or no `data/ma_pullback/` on it) is fine — the freshness check in the
   next step correctly blocks on a missing/stale export rather than silently
   proceeding with an empty watchlist.
3. Load `data/ma_pullback/watchlist.json` directly (or via `python3 -c "from
   lib.state import StateStore; import json;
   print(json.dumps(StateStore('data/ma_pullback').load_watchlist()))"`) —
   this is the `"watchlist"` field of the payload built in step 5 below.

## 1. Finviz freshness check + candidate load

```bash
python3 scripts/check_ma_universe_screen.py --config config/ma_pullback_strategy.yaml --data-dir data/ma_pullback
```

This checks `data/ma_pullback/finviz_export.csv` (the MA agent's own export,
genuinely separate from the stochastic system's — built in the Finviz Elite
UI with the screen documented in the README: **closed** at or above the
52-week high that day (a closing-price new-high scan, not an intraday-high
one — 2026-08-17, at the user's direction), average volume > 500k, price >
$5) isn't stale or missing, loads it, and prints the candidate list to
stdout. **If it exits non-zero** (stale or missing export): report
clearly that a fresh export needs to be published via `scripts/
publish_ma_finviz_export.sh` (run locally by the user) before this scan can
proceed — don't fabricate a candidate list, don't retry silently. This
already logged `ma_universe_screen_blocked` and sent an alert. Stop here,
skip the remaining steps — there's nothing to sync back.

**If it succeeds but returns zero candidates**: already logged
`ma_universe_screen_empty` and alerted. Continue anyway (see step 4 below —
a symbol already mid-retest on the watchlist still needs to be re-evaluated
today even with zero fresh candidates).

## 2. Fetch daily price history

Build the set of symbols needing daily history: **today's Finviz candidates
UNION every symbol already in `data/ma_pullback/watchlist.json`** — a symbol
mid-retest must keep being tracked even if it no longer prints as a fresh
52-week high today and drops off the Finviz list (see `lib.ma_breakout`'s
module docstring for why: overwriting a live breakout/retest state with
nothing just because today's snapshot moved on would silently lose real
progress toward an entry).

For each symbol in that set: `get_equity_historicals(symbols=[...],
interval="day", start_time=<~2 years back>, adjustment_type="split")`. Batch
up to 10 symbols per call, same bounded-concurrency approach
`hourly-signal-check.md` uses for its own candidate sweep (parallel tool
calls within one turn, not subagents — see that skill's section 5 for the
full reasoning if this list is large). ~2 years of history is enough margin
for both the 252-day breakout lookback and the 200-day SMA's own warmup
period (`config["breakout"]["lookback_days"]` +
`config["trend"]["sma_slow"]`, with room to spare) — check the `interpolated`
flag on returned bars and skip (log why) any symbol whose recent bars come
back synthetic, same convention as the stochastic system's fetches.

Shape each symbol's response into `{"dates": [...], "high": [...], "low":
[...], "close": [...], "volume": [...]}` (oldest-first) for the payload
below.

## 3. Run the scan

```bash
echo '{"finviz_candidates": [...], "price_history": {...}, "watchlist": {...current data/ma_pullback/watchlist.json...}}' | python3 scripts/check_ma_daily_scan.py --config config/ma_pullback_strategy.yaml --data-dir data/ma_pullback
```

This calls `lib.ma_breakout.update_watchlist_entry` per symbol and returns
the updated watchlist — a symbol's entry either advances (fresh breakout,
extension confirmed, retest seen, or a failed-breakout/age-out drop) or
passes through unchanged if there's nothing new to record. It logs
`ma_breakout_tracked` (fresh breakout — including overwriting a prior
failed/aged-out entry), `ma_retest_seen`, `ma_breakout_failed`, and
`ma_watchlist_dropped` events per meaningful transition, plus one summary
`ma_daily_scan` event. **This script writes no state to disk itself and
places no orders** — persist the result yourself in the next step.

## 4. Persist

`data/ma_pullback/watchlist.json` = the script's stdout output (atomic write
— either write the file directly, or run a one-off `python3 -c "from
lib.state import StateStore; import json, sys;
StateStore('data/ma_pullback').save_watchlist(json.load(sys.stdin))"` fed the
script's output).

## 5. Sync state out

`bash scripts/sync_state.sh push` — commits the updated
`data/ma_pullback/watchlist.json` (and today's log file, and the freshly
published `finviz_export.csv` if this is the first run since a publish) to
`bot-state`, so `hourly-ma-signal-check` sees it on its next cycle. Do this
even on a zero-candidates day. **If it exits non-zero**: same handling as
`daily-universe-screen.md`'s step 4 — `send_alert("state_sync_conflict",
"<the script's own stderr>")` and report it clearly; don't retry in a loop,
don't guess a resolution.

## 6. Report

Report back: symbols checked, how many are newly tracked (fresh breakout)
vs. progressing (retest seen this run) vs. dropped (failed or aged out), and
how many are currently eligible for entry (`eligible_for_entry: true`) —
this is exactly the set `hourly-ma-signal-check` will evaluate for a reclaim
trigger starting with the next scheduled cycle.

## Notes

- Watchlist state is long-lived (a symbol can sit here for weeks through a
  breakout, a pullback, and a retest) — this is deliberately NOT reset each
  morning the way the stochastic system's `pending_entries.json` is; see
  `lib/state.py`'s `watchlist_path` docstring for why it's a distinct
  concept from `candidates.json`.
- No earnings/catalyst filtering happens in this scan at all — earnings
  avoidance for this agent happens only on the tiny confirmed-entry
  shortlist, live, right before buying (see `hourly-ma-signal-check.md`),
  same pattern as the stochastic system.
- `scripts/sync_state.sh` never touches this branch's code history — safe to
  run from anywhere in the repo, same as every other use of it in this repo.
