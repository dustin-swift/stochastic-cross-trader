---
name: daily-universe-screen
description: Run once per day (before market open). Builds today's candidate list for the hourly stochastic pullback strategy from a manually-exported Finviz Elite CSV, excluding names with earnings reports too close (catalyst avoidance).
---

# Daily Universe Screen

Spec: `~/Downloads/hourly-stochastic-strategy-spec (1).md` §2, §6a. Plan: `/Users/dustinrowley/.claude/plans/elegant-brewing-phoenix.md`.

Purpose: refresh `data/candidates.json` — the list the hourly signal check
scans against all day. Screening itself is **Finviz-backed and fully
manual**: the user builds and exports the screen themselves in the Finviz
Elite web UI (market cap, price, volume, % off 52-week high, price vs SMA50
— see README for the exact filter setup), then publishes it locally via
`scripts/publish_finviz_export.sh` (see below — this skill has no access to
the user's local filesystem, it runs as a scheduled cloud routine from a
fresh git clone each time). This skill's own job is: sync state, run the
script that reads that export, layer an earnings/catalyst exclusion on top
of it (the one piece that does need a Robinhood MCP call), then sync state
back out.

## State sync (read this first)

This routine runs in an isolated cloud checkout with no memory of previous
runs and no access to the user's Mac. All persistent state (`data/` —
`candidates.json`, `finviz_export.csv`, logs) lives on a dedicated
`bot-state` branch, not in the routine's working tree by default. **Step 1
below is mandatory, not optional** — skipping it means operating on a stale
or missing Finviz export.

## Why the earnings check

The resting protective stop (`stop_market`) can only be placed regular-hours
— a hard Robinhood platform constraint, not a config choice. A pre-market or
after-hours earnings gap gets zero stop protection until regular hours
resume, and the eventual fill can land well below the intended stop. Since
there's no way to protect against the gap itself, the mitigation is not
opening a new position on the exact day it would get force-exited again
almost immediately. See `lib/catalysts.py` and `config/strategy.yaml`'s
`catalysts` section.

**The rule is BMO/AMC-aware (2026-07-30), not a fixed day-count window**: a
report's exit_date (the last trading day it's safe to hold through the close
of) is D-1 for a before-market-open (BMO) report on date D, or D itself for
an after-market-close (AMC) report — since the AMC session that day isn't
affected by news that lands after it closes. Unknown/missing timing defaults
conservatively to BMO (D-1). A new candidate is blocked **only on the
exit_date itself** — a single-day buffer, not the old flat 5-day window —
so entries on earlier days still capture the run-up into the report.

## Steps

1. **Sync state in**: `bash scripts/sync_state.sh pull` — populates `data/` (including whatever Finviz export was last published) from the `bot-state` branch. If this is a genuinely fresh setup with no `bot-state` branch yet, it logs that and continues with an empty `data/` — the freshness check in step 2 will then correctly block on a missing export rather than silently proceeding.
2. Run the screen once, without earnings filtering, to get the sector-capped
   candidate list:
   ```bash
   python3 scripts/check_universe_screen.py
   ```
   - **If it exits non-zero** (stale or missing `data/finviz_export.csv`): report clearly that the Finviz Elite export needs to be re-run and re-published (via `scripts/publish_finviz_export.sh`, run locally by the user) before the daily screen can proceed. Don't try to work around it, don't fabricate a candidate list, don't retry silently — this already logged a `universe_screen_blocked` event and sent an alert. Stop here — skip steps 3-5, there's nothing to sync back.
   - **If it succeeds but returns zero candidates**: it already logged `universe_screen_empty` and alerted. Report this clearly — don't treat it as a normal "nothing to do" case, since it usually means either a genuinely unusual market or a filter that needs revisiting. Continue to steps 3-5 regardless (the empty `candidates.json` is itself real state worth syncing back, so the hourly check sees "zero candidates today" rather than yesterday's stale list).
   - Otherwise, note the returned symbol list — this is what gets earnings-checked next.
3. For each symbol in that list, `get_earnings_results(symbol=...)` (one call per symbol — this tool is single-symbol only; batch the calls in parallel). Build a JSON object mapping symbol -> a list of `{"date": report.date, "timing": report.timing}` objects from the response (past and future both fine — `lib.catalysts` only looks at reports on or after today). **Include `timing`** ("am"=BMO, "pm"=AMC) — omitting it falls back to the conservative BMO default, which is safe but can needlessly cost a day of run-up on AMC names, exactly the thing this rule was changed to avoid. If a call fails for a symbol, simply omit that symbol from the map — that's the intended "not successfully checked" signal (see script docstring), not an error to work around.
4. Re-run the screen, this time passing that map, so the final `data/candidates.json` reflects the earnings exclusion:
   ```bash
   echo '<earnings map from step 3>' | python3 scripts/check_universe_screen.py --earnings-input -
   ```
   This re-reads the same Finviz CSV and re-applies the sector cap (cheap, no network) — it's the earnings filter on top that's new this run. It overwrites `data/candidates.json` with the final, earnings-filtered list and logs `universe_screen` again (plus `universe_screen_earnings_excluded` if anything got excluded).
5. **Sync state out**: `bash scripts/sync_state.sh push` — commits the updated `data/candidates.json` (and today's log file) to `bot-state` so the hourly-signal-check routine sees it. Do this even on a zero-candidates day (see step 2's second bullet) — do **not** do this if step 2 exited non-zero (blocked run), since there's no new state to persist and pushing the old CSV back is a no-op anyway (`sync_state.sh push` correctly no-ops on an unchanged tree). **If it exits non-zero** (2026-08-04): the script already retries through ordinary concurrent-update races on its own; a surfaced failure means either it exhausted retries or hit a genuine same-data conflict with another routine run and refused to guess. `send_alert("state_sync_conflict", "<the script's own stderr>")` and report it clearly rather than treating it as a normal error to shrug off — the hourly-signal-check routine would otherwise keep trading against a stale candidate list.
6. Report the final candidate list and per-sector counts back. If `universe_screen_earnings_excluded` fired, mention which symbols were dropped and why (`too_close` = today is that report's exit_date; `unknown` = the get_earnings_results call for that symbol failed or was skipped) — an `unknown` exclusion is worth a second look if it's happening a lot, since it usually means a fetch problem, not an earnings problem.

## Notes

- This *replaces* the previous day's `data/candidates.json` — the hourly check only ever looks at today's file.
- Earnings exclusion happens **after** the sector cap, not before — checking earnings for the full raw Finviz universe (which can be 50-100+ rows) would reintroduce the same per-symbol-call scaling problem that Finviz replaced `create_scan` for in the first place. The tradeoff: an excluded slot isn't backfilled from the same sector. Revisit if this meaningfully shrinks the daily list in practice.
- Skipping step 3-4 entirely (just running step 2 alone) is a valid degraded mode — the script works fine with no `--earnings-input` at all, exactly as before this feature existed. Only do this deliberately (e.g. Robinhood MCP is unavailable that morning), not as a silent shortcut. Still sync state out afterward either way.
- `scripts/sync_state.sh` never touches this branch's code history — it reads/writes `data/` against `bot-state` via git plumbing (see the script's own comments) and is safe to run from anywhere in the repo.
