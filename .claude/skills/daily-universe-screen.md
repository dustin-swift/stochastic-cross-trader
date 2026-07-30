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
— see README for the exact filter setup). This skill's own job is: run the
script that reads that export, then layer an earnings/catalyst exclusion on
top of it (the one piece that does need a Robinhood MCP call).

## Why the earnings check

The resting protective stop (`stop_market`) can only be placed regular-hours
— a hard Robinhood platform constraint, not a config choice. A pre-market or
after-hours earnings gap gets zero stop protection until regular hours
resume, and the eventual fill can land well below the intended stop. Since
there's no way to protect against the gap itself, the mitigation is not
opening a new position too close to a known earnings date in the first
place. See `lib/catalysts.py` and `config/strategy.yaml`'s `catalysts`
section.

## Steps

1. Run the screen once, without earnings filtering, to get the sector-capped
   candidate list:
   ```bash
   python3 scripts/check_universe_screen.py
   ```
   - **If it exits non-zero** (stale or missing `data/finviz_export.csv`): report clearly that the Finviz Elite export needs to be re-run and re-saved before the daily screen can proceed. Don't try to work around it, don't fabricate a candidate list, don't retry silently — this already logged a `universe_screen_blocked` event and sent an alert. Stop here.
   - **If it succeeds but returns zero candidates**: it already logged `universe_screen_empty` and alerted. Report this clearly — don't treat it as a normal "nothing to do" case, since it usually means either a genuinely unusual market or a filter that needs revisiting. Stop here (no candidates to earnings-check).
   - Otherwise, note the returned symbol list — this is what gets earnings-checked next.
2. For each symbol in that list, `get_earnings_results(symbol=...)` (one call per symbol — this tool is single-symbol only; batch the calls in parallel). Build a JSON object mapping symbol -> the list of report dates from the response (past and future both fine — `lib.catalysts` only looks at dates on or after today). If a call fails for a symbol, simply omit that symbol from the map — that's the intended "not successfully checked" signal (see script docstring), not an error to work around.
3. Re-run the screen, this time passing that map, so the final `data/candidates.json` reflects the earnings exclusion:
   ```bash
   echo '<earnings map from step 2>' | python3 scripts/check_universe_screen.py --earnings-input -
   ```
   This re-reads the same Finviz CSV and re-applies the sector cap (cheap, no network) — it's the earnings filter on top that's new this run. It overwrites `data/candidates.json` with the final, earnings-filtered list and logs `universe_screen` again (plus `universe_screen_earnings_excluded` if anything got excluded).
4. Report the final candidate list and per-sector counts back. If `universe_screen_earnings_excluded` fired, mention which symbols were dropped and why (`too_close` = real upcoming report within the window; `unknown` = the get_earnings_results call for that symbol failed or was skipped) — an `unknown` exclusion is worth a second look if it's happening a lot, since it usually means a fetch problem, not an earnings problem.

## Notes

- This *replaces* the previous day's `data/candidates.json` — the hourly check only ever looks at today's file.
- Earnings exclusion happens **after** the sector cap, not before — checking earnings for the full raw Finviz universe (which can be 50-100+ rows) would reintroduce the same per-symbol-call scaling problem that Finviz replaced `create_scan` for in the first place. The tradeoff: an excluded slot isn't backfilled from the same sector. Revisit if this meaningfully shrinks the daily list in practice.
- Skipping step 2-3 entirely (just running step 1 alone) is a valid degraded mode — the script works fine with no `--earnings-input` at all, exactly as before this feature existed. Only do this deliberately (e.g. Robinhood MCP is unavailable that morning), not as a silent shortcut.
