---
name: daily-universe-screen
description: Run once per day (before market open). Builds today's candidate list for the hourly stochastic pullback strategy from a manually-exported Finviz Elite CSV.
---

# Daily Universe Screen

Spec: `~/Downloads/hourly-stochastic-strategy-spec (1).md` §2, §6a. Plan: `/Users/dustinrowley/.claude/plans/elegant-brewing-phoenix.md`.

Purpose: refresh `data/candidates.json` — the list the hourly signal check
scans against all day. Screening itself is **Finviz-backed and fully
manual**: the user builds and exports the screen themselves in the Finviz
Elite web UI (market cap, price, volume, % off 52-week high, price vs SMA50
— see README for the exact filter setup), then publishes it locally via
`scripts/publish_finviz_export.sh` (see below — this skill has no access to
the user's local filesystem, it runs as a scheduled cloud routine against a
checkout of `main` that is *supposed* to be fresh each time but has been
observed to lag from a cached snapshot — see step 1 below). This skill's own
job is just: sync state, run the one script that reads that export, sync
state back out — a single pass, no Robinhood MCP calls at all.

**No earnings/catalyst filtering happens in this screen** (2026-08-14, at
the user's direction): earnings avoidance for entries happens only on the
tiny handful of candidates that actually confirm a stochastic entry signal
in a given hourly cycle, via a live `get_earnings_results` check right
before buying — see `scripts/filter_entry_earnings.py` and the
hourly-signal-check / daily-stochastic-check skills. This screen doesn't
need to know about earnings dates at all.

## State sync (read this first)

This routine runs in an isolated cloud checkout with no memory of previous
runs and no access to the user's Mac. All persistent state (`data/` —
`candidates.json`, `finviz_export.csv`, logs) lives on a dedicated
`bot-state` branch, not in the routine's working tree by default. **Steps 1-2
below are mandatory, not optional** — skipping the checkout-freshness check
risks running against a stale codebase, and skipping the state sync means
operating on a stale or missing Finviz export.

## Steps

1. **Verify the code checkout is fresh (mandatory, do this before anything else)**: `git fetch origin main && git rev-parse HEAD` vs `git rev-parse origin/main`. If they differ, run `git reset --hard origin/main` before proceeding. This cloud environment has been observed to boot from a cached snapshot that can lag `main` by one or more commits (confirmed 2026-08-07 on the hourly-signal-check routine — a scheduled run reported two committed-and-pushed files as entirely absent from git history, which turned out to be a stale checkout, not a real repo gap) — don't assume "fresh git clone each time" actually holds. Log a `stale_checkout_detected` event if a mismatch was found and corrected.
2. **Sync state in**: `bash scripts/sync_state.sh pull` — populates `data/` (including whatever Finviz export was last published) from the `bot-state` branch. If this is a genuinely fresh setup with no `bot-state` branch yet, it logs that and continues with an empty `data/` — the freshness check in the next step will then correctly block on a missing export rather than silently proceeding.
3. Run the screen:
   ```bash
   python3 scripts/check_universe_screen.py
   ```
   This loads the CSV and applies the sector cap — no `--earnings-input` needed, no MCP calls made.
   - **If it exits non-zero** (stale or missing `data/finviz_export.csv`): report clearly that the Finviz Elite export needs to be re-run and re-published (via `scripts/publish_finviz_export.sh`, run locally by the user) before the daily screen can proceed. Don't try to work around it, don't fabricate a candidate list, don't retry silently — this already logged a `universe_screen_blocked` event and sent an alert. Stop here — skip steps 4-5, there's nothing to sync back.
   - **If it succeeds but returns zero candidates**: it already logged `universe_screen_empty` and alerted. Report this clearly — don't treat it as a normal "nothing to do" case, since it usually means either a genuinely unusual market or a filter that needs revisiting. Continue to steps 4-5 regardless (the empty `candidates.json` is itself real state worth syncing back, so the hourly check sees "zero candidates today" rather than yesterday's stale list).
4. **Sync state out**: `bash scripts/sync_state.sh push` — commits the updated `data/candidates.json` (and today's log file) to `bot-state` so the hourly-signal-check routine sees it. Do this even on a zero-candidates day (see step 3's second bullet) — do **not** do this if step 3 exited non-zero (blocked run), since there's no new state to persist and pushing the old CSV back is a no-op anyway (`sync_state.sh push` correctly no-ops on an unchanged tree). **If it exits non-zero** (2026-08-04): the script already retries through ordinary concurrent-update races on its own; a surfaced failure means either it exhausted retries or hit a genuine same-data conflict with another routine run and refused to guess. `send_alert("state_sync_conflict", "<the script's own stderr>")` and report it clearly rather than treating it as a normal error to shrug off — the hourly-signal-check routine would otherwise keep trading against a stale candidate list.
5. Report the final candidate list and per-sector counts back.

## Notes

- This *replaces* the previous day's `data/candidates.json` — the hourly check only ever looks at today's file.
- Each successful run of `check_universe_screen.py` also resets `data/pending_entries.json` to `{}` — the dual-cross entry state machine (spec §3, 2026-08-04 revision, see `lib.signals.advance_pending_entry`) is intraday-only: a %K-crossed-but-%D-hasn't setup from a prior day shouldn't silently carry into today's first hourly check.
- `--earnings-input` still exists on `check_universe_screen.py` as a raw capability — not used by this skill or any scheduled routine.
- `scripts/sync_state.sh` never touches this branch's code history — it reads/writes `data/` against `bot-state` via git plumbing (see the script's own comments) and is safe to run from anywhere in the repo.
