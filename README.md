# Hourly Stochastic Pullback Trader

Implementation of the strategy in `~/Downloads/hourly-stochastic-strategy-spec (1).md`:
buy fundamentally strong stocks (near 52-week highs, in confirmed uptrends)
during a short-term pullback, entering once an hourly stochastic %K/%D
dual-cross above 20 confirms the reversal (see Entry logic below), with an
ATR-based resting stop and a state-aware signal exit (see
Exit logic below — extended overbought runs are held through whipsaws instead
of exiting on the first crossover). The daily universe screen is Finviz Elite
(manual export, which also supplies daily ATR(14) directly — see "Finviz
Elite manual screen" below, no live indicator fetch needed for it); everything
else — hourly bars, account data, and order execution — goes through the
Robinhood Agentic MCP connector (`.mcp.json`).

Design plan: `/Users/dustinrowley/.claude/plans/elegant-brewing-phoenix.md`.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## Repo layout

- `config/strategy.yaml` — every tunable (screening path, stochastic
  settings, ATR stop multiplier, position sizing, daily-loss circuit breaker,
  order-fill polling timeout, alert provider, and the `live` dry-run/live
  switch). Edit this, not code, to retune.
- `providers/finviz.py` — reads the manually-exported Finviz Elite CSV from
  disk and checks it isn't stale. No network calls, no auth key.
- `lib/` — pure, unit-tested computation: indicators, signal logic, sector
  cap, whole-share entry sizing (`lib/sizing.py` — see Whole-share sizing
  below), entry-order fill/reject/timeout decisions, state (JSON-backed
  candidates/positions/daily P&L), risk circuit breaker, config loading,
  JSONL event logging, earnings/catalyst avoidance (`lib/catalysts.py`), and
  the historical backtest engine (`lib/backtest.py` — see Backtesting below).
  No network calls anywhere in `lib/` except `lib/alerts.py`'s webhook POST
  (mocked in every test).
- `scripts/` — thin CLIs around `lib/`/`providers/` that the agent skills
  shell out to: `check_universe_screen.py` (Finviz CSV → candidates.json,
  optionally earnings-filtered), `check_hourly_signals.py` (bars → entry/exit
  signals, earnings-aware), `run_backtest.py` (historical replay — see
  Backtesting below), `check_circuit_breaker.py`, `evaluate_order_fill.py`,
  `record_stop_failure.py` (the three alert-wired order-lifecycle helpers —
  see Alerting below).
- `.claude/skills/daily-universe-screen.md`, `.claude/skills/hourly-signal-check.md`
  — step-by-step runbooks for an agent invocation (Robinhood MCP calls +
  the scripts above). These are what actually run each cycle, whether
  triggered manually or by a scheduled cloud agent.
- `data/` — gitignored on `main`. `finviz_export.csv` (your manual export —
  see below), `candidates.json` (today's list), `pending_entries.json`
  (intraday dual-cross entry state — see Entry logic below),
  `last_cycle_at.json` (missed-cycle guard — see Entry logic below),
  `positions.json` (open positions only — see `trade_history.json` below for closed trades),
  `trade_history.json` (every closed trade — stop-out, signal exit,
  overbought-hold exit, or earnings-forced exit — with entry/exit price, P&L,
  exit reason, and the %K/%D readings on both the trigger bar and the prior
  bar for entry and exit alike — see Trade-detail fields below; this is what
  the dashboard reads for closed-trade drill-down, since `positions.json`
  drops a trade the moment it closes),
  `daily_pnl.json`, `logs/YYYY-MM-DD.jsonl` (every signal check, decision,
  order event, and alert). Not committed to `main` — see Cloud routines &
  state persistence below for where this data actually lives when running on
  a schedule.
- `scripts/sync_state.sh`, `scripts/publish_finviz_export.sh` — the
  state-persistence and Finviz-publish mechanics for cloud routines (see
  below).

## Finviz Elite manual screen

Screening (market cap, price, average volume, % off 52-week high, price vs
SMA50) is **not automated** — you build and export it yourself:

1. In the Finviz Elite screener, set filters:
   - Market Cap: over $2B
   - Price: over $10
   - Average Volume: over 750K
   - 50-Day Simple Moving Average: price above SMA50
   - 52-Week High/Low: within 15% of the 52-week high
2. Export the results to CSV (Elite plans include CSV export). Include the
   **Average True Range** column — `scripts/check_universe_screen.py` parses
   it straight into each candidate's `atr14` (daily ATR(14), the standard
   convention), which the live entry lifecycle then uses directly for the
   resting stop-loss calculation with no live indicator fetch needed. A
   symbol without that column (or a blank cell) just carries `atr14: null`
   through — its estimated stop won't show in dry-run output, and a live
   entry signal on it gets logged and skipped rather than guessing a stop
   distance (see `.claude/skills/hourly-signal-check.md`).
3. Publish it: `bash scripts/publish_finviz_export.sh /path/to/your/export.csv`. This is the step that actually matters when the daily screen runs as a scheduled cloud routine (see Cloud routines & state persistence below) — the routine runs in a fresh clone with no access to your Mac, so simply saving the file locally to `data/finviz_export.csv` isn't enough on its own; the script pulls the latest `bot-state`, copies your export into `data/`, and pushes it back so the next cloud run can see it. (If you're only ever running the skills manually, by hand, from this machine, saving the file to `data/finviz_export.csv` directly also works — `screening.finviz_csv_path` is configurable in `config/strategy.yaml` — but the script is the supported path once anything is scheduled.)
4. Re-publish whenever you want to refine the filters, or when the daily screen reports the file is stale.

**Staleness check**: the daily-universe-screen skill refuses to run on a stale
export. "Stale" is trading-day-aware, not just "older than N hours" — a
Friday export is treated as valid through Monday, and goes stale starting
Tuesday. **Known limitation**: this only accounts for weekends, not market
holidays — an export from before a Monday holiday will incorrectly look
fresh on the Tuesday after it. Re-export after any holiday to be safe.

## Running a cycle manually

There's no CLI entry point that does the whole cycle end-to-end by itself —
the Robinhood MCP calls (hourly bars, order placement) only work from an
authenticated agent session. To run a cycle, tell the agent to follow one of
the skills:

- "Run the daily-universe-screen skill" — once per day, before market open. Requires a fresh `data/finviz_export.csv` (see above).
- "Run the hourly-signal-check skill" — hourly during market hours.

Both are safe to run manually as often as you like during testing; nothing
about them assumes a particular trigger source.

The universe-screen script can also be run standalone for debugging:

```bash
python3 scripts/check_universe_screen.py
```

## Dry-run vs. live

`config/strategy.yaml`'s `live` flag controls everything:

- `live: false` (default) — every cycle computes signals and logs exactly
  what *would* have been ordered (entries, stops, exits) to
  `data/logs/YYYY-MM-DD.jsonl`, but **no order tool is ever called**. This is
  how the system ships and how it should stay until you've reviewed several
  real cycles of dry-run output against live market data.
- `live: true` — the hourly-signal-check skill places real orders: a
  whole-share market buy (see Whole-share sizing below), polled through to a
  fill/reject/timeout decision (see Order-lifecycle robustness below),
  followed immediately by a real resting `stop_market` sell order (not a
  soft "check next hour" stop). Only flip this after you've reviewed
  dry-run logs and are ready to trade the account for real.

To flip live trading on: edit `live: true` in `config/strategy.yaml`. That's
the only change needed — no code changes, no redeploy.

## Entry logic: dual %K/%D-cross-20 confirmation (spec §3, 2026-08-04)

An entry used to fire the instant %K crossed above `stochastic.oversold_threshold`
(default 20) and above %D in the same bar — but %D, a slower trailing average
of %K, was very often still well below 20 at that exact moment (confirmed
live on LEVI: entered with %K=20.52 but %D=15.53), meaning a brief %K spike
alone could trigger without any real confirmation that price was genuinely
reversing. At the user's direction, entries now require **both** %K and %D to
independently cross above the threshold before firing — the slower %D
crossing is what distinguishes a sustained reversal from a quick spike.

Because %D lags, this can't be decided on a single bar: %K crossing above the
threshold starts a *pending* setup (`data/pending_entries.json`, `{symbol:
{"k_at_cross": float}}`) that persists across hourly cycles until one of
three things happens (see `lib.signals.advance_pending_entry`):

- **%D also crosses above the threshold, with %K still at or below
  `stochastic.k_invalidate_max` (default 55)** — the entry fires.
- **%D crosses above the threshold, but %K has already run past
  `k_invalidate_max`** — invalidated as "too extended": a %D confirmation
  arriving that late isn't confirming a reversal anymore, it's just lagging a
  move that already happened.
- **%K drops back below the threshold before %D ever confirms** —
  invalidated; the reversal attempt failed. No expiry timer — this is the
  only other way a pending setup un-pends without firing.

Pending state is intraday-only: `scripts/check_universe_screen.py` resets
`data/pending_entries.json` to `{}` on every successful run, so a setup mid-
confirmation never silently carries into the next trading day.

**Basing-pattern requirement (`stochastic.entry_lookback_bars`, 2026-08-06,
at the user's direction):** a literal "%K crosses above 20" already implies
the immediately-preceding bar was below 20, so requiring just that one bar
adds almost nothing — a single bar dipping under 20 and immediately
bouncing can be ordinary noise in a downtrend, not a real reversal. Live
review found several losing entries (CUZ, CNQ, DNTH, DINO) all confirmed
with %D barely above 20 (21-26 range) off exactly that kind of single-bar
dip. `entry_lookback_bars` (default 2) now requires **all** of the N bars
immediately before the crossing bar to have been oversold, not just the
crossing bar's own predecessor — a genuine short basing/consolidation run,
not a brush past the line. Raised conservatively from 1 to 2 as a first
step; consider 3 if `signal_exit` frequency is still high after review (see
Trade-detail fields above for the data to make that call from). This
supersedes an earlier version of this feature that used `.any()` instead of
`.all()` across the lookback window, which made widening it a no-op above 1
— see `lib/signals.py`'s module docstring for the full history.

**Missed-cycle guard (2026-08-04, confirmed live on DNTH/ILF/PCAR):** the
crossing check above only ever compares the two most recent bars it was
handed — it has no way to know how much real time actually separates them.
On 2026-08-04, an MCP connector naming mismatch made two consecutive
scheduled cycles silently do nothing (no error, no state change — see the
incident writeup in `config/strategy.yaml`'s `stochastic.max_cycle_gap_minutes`
comment). By the time the next cycle ran, its two-bar comparison spanned
several real hours instead of one, satisfying the crossing condition on
paper while %K/%D had already been running well past 20 in the real market —
not the fresh reversal the rule exists to catch. `check_hourly_signals.py`
now tracks its own last-completed-run time in `data/last_cycle_at.json`
(read/written directly, no skill-doc step needed); if the gap since then
exceeds `stochastic.max_cycle_gap_minutes` (default 90 — 1.5x the normal
hourly cadence), any entry that would otherwise fire this cycle is
suppressed and logged as `entry_suppressed_stale_cycle` instead. Pending
state still advances normally either way — only the entry itself is held
back for that cycle. Exits are never affected by this guard: missing a
chance to close risk is worse than a late one, unlike opening new risk on a
possibly-stale read.

## Market-regime filter (config["market_filter"], 2026-08-06)

Only opens NEW positions while the broad market isn't in a short-term
decline — gates section 5 (entries) of the hourly-signal-check skill,
exactly like the circuit breaker; exits and existing positions are never
affected, and a failing check skips the entire candidate fetch for the
cycle too (computed early, section 2, before section 5's expensive fetch —
no reason to spend the latency on a fetch we won't act on). Built after a
live review found two positions (CNQ, DINO) enter within a minute of each
other and reverse for a loss within seconds of each other the next day —
correlated, not independent, failures.

**Revised twice the same day.** v1 used HOURLY bars (20/200-period SMAs, a
multi-week regime read) requiring the fast SMA above the slow SMA *and*
genuinely rising. On reflection this couldn't actually catch what it was
built for: a single steep down day, even a real one, usually isn't enough
to pull a 3-day average below a 6-week one inside an otherwise-intact
longer uptrend — a slow filter answers "is the market in an uptrend," not
"is there a selloff happening right now," and those are different
questions with different lookback horizons.

v2 (current) switches `lib.market_filter.market_trend_intact` to **MINUTE**
bars, same `fast_sma_period: 20` / `slow_sma_period: 200`, now spanning
roughly 20 minutes and ~3.3 hours — a same-day momentum read instead of a
multi-week one — and drops the rising requirement (`require_rising: false`,
a plain fast-above-slow comparison; minute-level averages are noisy enough
that a separate slope check adds little). **Known trade-off, not assumed
away**: at this timeframe the filter can also trip during an ordinary
intraday pullback-and-bounce, not just a genuine steep selloff — exactly
the kind of dip this strategy exists to buy into. Worth watching after some
live cycles whether it's blocking good entries too often, not just bad
ones; `require_rising` and the periods are one-line config edits if it
needs retuning.

**SPY over QQQ**: the two aren't always correlated (QQQ skews tech/growth),
so picking one is a real trade-off either way — SPY was chosen as the more
standard broad-market proxy (`config["market_filter"]["symbol"]`). Revisit
(or require both to agree) if this turns out to miss regime shifts QQQ
would have caught, or vice versa.

Missing/insufficient bar history reads as `null`, treated the same as
`true` (don't block entries on missing data — the same convention as the
exit-side `trend_intact` filter). `market_filter.enabled: false` disables
the feature entirely without needing a live fetch.

## Trade-detail fields (2026-08-05)

Every entry/exit decision carries the confirming bar's %K/%D *and* the prior
bar's %K/%D — added at the user's request, specifically to support reviewing
each closed trade after the fact and figuring out what's working versus
what isn't, rather than just seeing an aggregate win rate.

- `check_hourly_signals.py`'s `entries[]`/`exits[]` output includes `k`/`d`
  (the trigger bar) and `prev_k`/`prev_d` (one hour earlier) for every
  decision — `None` on either pair when there wasn't enough bar history to
  look back, or for a forced `earnings_exit` (which bypasses the oscillator
  check entirely, so there's nothing to report).
- The hourly-signal-check skill writes `entry_k`/`entry_d`/`entry_prev_k`/
  `entry_prev_d` onto the position in `positions.json` the moment it opens,
  and passes `exit_k`/`exit_d`/`exit_prev_k`/`exit_prev_d` into
  `record_trade_close.py` the moment it closes (see that script's docstring
  for the input shape) — a `stop_out` exit has no exit-side reading, since
  the resting stop fired on its own between cycles, not off a fresh check.
- `trade_history.json` ends up with all eight fields per closed trade
  (`entry_k`, `entry_d`, `entry_prev_k`, `entry_prev_d`, `exit_k`, `exit_d`,
  `exit_prev_k`, `exit_prev_d`), alongside the price/P&L fields that were
  already there — see `lib.state.close_trade_record`. The dashboard's closed-
  trade detail drill-down (see Dashboard below) surfaces all eight per row.
- Positions/trades written before this feature existed simply carry these
  fields through as `None` — no backfill, no migration.

## Exit logic: overbought-hold refinement (spec §4)

Every open position tracks a `stochastic_state` in `positions.json`, alongside
its stop/entry data — `NORMAL` (the default on entry) or `OVERBOUGHT_HOLD`:

- **`NORMAL`**: exit is an ordinary bearish %K/%D crossover, same as the
  original single-condition rule.
- **`OVERBOUGHT_HOLD`**: entered the moment %K and %D are simultaneously >=
  `stochastic.overbought_threshold` (default 80). While in this state, all
  %K/%D crossovers are ignored — a stock can whipsaw the two lines against
  each other many times while pinned near 100 and still be working, and a
  naive stateless crossover exit would shake you out of exactly the kind of
  extended move this is meant to catch. Exit only fires once *both* lines
  drop back below the threshold.

This only changes the oscillator-based signal exit. **The resting ATR
stop-loss is completely unaffected** — it stays active and untouched in
either state, so a hard reversal while `OVERBOUGHT_HOLD` is still protected;
you're only suspending the momentum-based exit, not risk management. The
state machine lives in `lib/signals.py` (`update_stochastic_state`,
state-aware `exit_signal`); `scripts/check_hourly_signals.py` is stateless
per-invocation — it advances the state by one bar given whatever
`stochastic_state` the hourly-signal-check skill fed it, and returns it in
`position_states` for every open position each cycle so the skill can persist
it back to `positions.json`, whether or not that cycle produced an exit.

## Exit logic: trend-intact filter (config["trend_filter"], spec §4a)

A second, independent refinement to the same `NORMAL`-state crossover exit
above — this one motivated by a real observed pattern (a stock climbing
steadily while %K/%D whipsawed bearishly several times mid-move, each of
which would have exited a still-working position). Stochastic is a
mean-reversion/momentum oscillator and is well known to generate false
reversal signals during a genuinely trending move; the standard remedy is to
gate the oscillator's exit behind a trend filter and only honor it once the
trend itself has actually broken.

While a position is `stochastic_state: NORMAL`, a bearish %K/%D crossover is
suppressed whenever price is still above its own `trend_filter.sma_period`-bar
SMA (default 50) on the signal bar — computed fresh each cycle from the same
hourly bars already fetched for the stochastic calc, not a state field, not
persisted. `stochastic_state: OVERBOUGHT_HOLD` is **completely unaffected** —
still governed purely by the downside-80 rule from the section above — and
the hard ATR stop is unaffected by this filter exactly as it's unaffected by
overbought-hold. Missing/insufficient bar history for the SMA falls through
to the plain crossover check rather than trapping a position open
indefinitely on missing data. Optional in config — omitting the
`trend_filter` section entirely defaults to enabled with a 50-bar SMA, same
as setting it explicitly.

## Catalyst avoidance: earnings (config["catalysts"])

The resting protective stop (`stop_market`) can only be placed regular-hours
— a hard Robinhood platform constraint, not a config choice. A pre-market or
after-hours earnings gap gets zero stop protection until regular hours
resume, and the eventual fill can land well below the intended stop. Since
there's no way to protect against the gap itself, the mitigation is avoiding
known earnings dates around the exact moment the gap risk exists.

**The rule is BMO/AMC-aware (2026-07-30), not a fixed day-count window** —
see `lib/catalysts.py` for the implementation. Each report carries a
`timing` — `"am"` (before market open) or `"pm"` (after market close), from
`get_earnings_results`' `report.timing` — and the *exit_date* (the last
trading day it's safe to hold through the close of) is computed from it:

- **BMO** report on date D: the regular session on D already opens knowing
  the news, so exit_date = D - 1.
- **AMC** report on date D: the regular session on D is unaffected by the
  report (it lands after that session closes), so exit_date = D itself.
- **Unknown/missing timing** (e.g. legacy data, or a fetch that didn't
  include it): treated as BMO, exit_date = D - 1 — the more conservative
  choice, since this whole rule exists to protect against an unprotected
  gap.

Both sides of the check read off the same exit_date:

- **Hourly check (open positions)**: force-exited (`exit_reason:
  "earnings_exit"`) regardless of stochastic state — including while
  `OVERBOUGHT_HOLD`, since a stock can easily be holding overbought right
  into its earnings date — on or after the nearest upcoming report's
  exit_date. **On the exit_date itself, only on the last regular-session
  check of that day, not the first** (`config["catalysts"]
  ["forced_exit_utc_hour"]`, default 19 — 2026-08-04 fix, confirmed live on
  BALL: firing on the first check of exit_date instead of the last
  forfeited most of that day's run-up, exactly what holding through
  exit_date is meant to capture). A day already past exit_date (a missed
  close-of-day check) still force-exits immediately regardless of time.
  **Re-fetched only on the last scheduled cycle of the day, not every cycle**
  (2026-08-06, at the user's direction — `get_earnings_results` has no batch
  mode, so re-checking all open positions every hour was pure overhead: the
  force-exit itself was already gated to that same near-close cycle, so an
  earlier fetch could never be acted on any sooner anyway). Known trade-off:
  an already-overdue position (a whole day's cycles missed) is now only
  caught at the *next* last-cycle check instead of immediately — acceptable
  since healthy operation never reaches that branch in the first place.
- **Candidates**: blocked only on the exit_date itself — a **single-day
  buffer**, not a multi-day window. Entering on the exit_date would get
  force-exited again almost immediately (near-zero-value trade), but
  entering any day before that is intentionally allowed, so entries still
  capture the run-up into the report. (Prior to 2026-07-30 this was a flat
  5-day exclusion window, which was excessive and cost real run-up —
  changed at the user's direction after noticing it.) **Checked only for
  the tiny shortlist that already confirmed a stochastic signal, right
  before buying** (2026-08-06 — see `scripts/filter_entry_earnings.py`),
  not for the full ~300+ candidate list every cycle; that upfront fetch had
  been the dominant cost in a whole cycle's latency for almost no payoff,
  since only a handful of candidates ever actually signal.

`config["catalysts"]["enabled"]` (default `true`) is a single on/off switch
for the whole feature, both sides.

Both checks are **optional per call** — a symbol missing `earnings_report_dates`
entirely is handled differently depending on context (see `lib/catalysts.py`
and `scripts/check_hourly_signals.py` docstrings): the daily screen treats
"not checked" conservatively as excluded, but an open position with no
earnings data falls through to normal signal logic rather than being force-
sold — a data-fetch hiccup shouldn't liquidate a live position. If Robinhood
MCP is unavailable, both scripts still work fine with no earnings data at
all, exactly as before this feature existed — it's additive, not required.

## Whole-share sizing (config["sizing"])

Entries are sized to a whole number of shares, not a dollar amount. This
replaced dollar-based fractional entries after a live, confirmed platform
limitation (2026-07-30): the broker rejects *any* stop-type order
(`stop_market` or `stop_limit`) against a fractional-share quantity
("Invalid trigger for fractional order"), independent of `time_in_force` — a
fractional fill can never get a resting protective stop, full stop. Since
the whole safety model depends on that stop existing, entries now go through
`lib.sizing.entry_share_quantity(price, per_trade_usd, max_price_per_share)`:

- **price < `per_trade_usd`**: buy `round(per_trade_usd / price)` shares —
  whichever whole-share count lands closest to the target spend, above or below.
- **`per_trade_usd` <= price <= `max_price_per_share`**: buy exactly 1 share.
- **price > `max_price_per_share`**: skip the entry entirely for this cycle
  (logged as `entry_skipped_price_cap`) rather than buying 1 share regardless
  of cost — a candidate this expensive never makes it into `entries` at all.

`max_price_per_share` is set to 1.5x `per_trade_usd` by default, chosen so
the round() branch above can never land on a share costing more than the cap,
and never rounds down to 0 shares for any price this doesn't already skip.

## Order-lifecycle robustness & alerting

Two things worth knowing about since this runs unattended on a schedule:

**Entry-order polling** (`config.order_lifecycle`): after a market buy is
placed, the system polls for a fill every `poll_interval_seconds` up to
`poll_timeout_seconds` (defaults: 5s / 30s). Three non-happy-path outcomes
are handled explicitly, not left implicit:
- **Rejected/cancelled/failed**: no position written, alerted.
- **Timeout unfilled**: the order is cancelled explicitly (not left resting), no position written, alerted.
- **Partial fill**: if still partial at timeout, the filled quantity becomes the position (stop computed from that quantity) and the unfilled remainder is cancelled — logged either way.

**Alerts**: a narrow, deliberate list of critical events — not routine
activity — post to a Slack or Discord webhook (`lib/alerts.py`), so they
reach you in real time instead of sitting in a log file:
- `universe_screen_blocked` — stale/missing Finviz CSV.
- `universe_screen_empty` — screen ran fine but found zero candidates.
- `circuit_breaker_triggered`.
- `entry_order_rejected_or_timeout`.
- `stop_placement_failed` — the one failure mode this whole design exists to avoid: a live position with no protective stop. Always alerts, no exceptions.

**Setup**: create a Slack or Discord incoming webhook, then:

```bash
export ALERT_WEBHOOK_URL="https://hooks.slack.com/services/..."  # or a Discord webhook URL
```

Set `alerts.provider` in `config/strategy.yaml` to `slack` or `discord` to
match (this selects the JSON payload shape — Slack expects `{"text": ...}`,
Discord expects `{"content": ...}`). The webhook URL itself is never stored
in `config/strategy.yaml` or committed — env var only. If `ALERT_WEBHOOK_URL`
isn't set, alerts are silently skipped (logged locally as a warning, never
crashes the pipeline) — so set it before relying on this for real trading.

Test it once before trusting it:
```bash
python3 -c "from lib.alerts import send_alert; send_alert('test', 'hello from setup')"
```

## Backtesting

`lib/backtest.py` (via `scripts/run_backtest.py`) is a standalone research
tool — **not** part of the live/dry-run pipeline, reads no live account
state, places no orders. It replays the exact same entry/exit logic used
live (`lib/signals.py`) bar-by-bar across a chronologically merged multi-
symbol timeline, given historical bars + an ATR series you've already
fetched via Robinhood MCP:

```bash
python3 scripts/run_backtest.py --input backtest_data.json
```

Input shape: `{"symbol_data": {"AAPL": {"bars": [...], "atr_series": [...],
"earnings_report_dates": [...]}, ...}}` (bars oldest-first; `atr_series` and
`earnings_report_dates` are both optional per symbol). Output: every closed
trade (with entry/exit price, P&L, and max favorable/adverse excursion —
`mfe_pct`/`mae_pct`, the best/worst price seen during the hold, useful for
judging whether a tighter stop or a trailing stop would plausibly have
helped), any still-open positions, and summary stats (win rate, total P&L,
avg win/loss, max drawdown, breakdown by exit reason). Also written to
`<data-dir>/backtest_results.json`.

Two things it can simulate that the live pipeline doesn't (yet):
- **Trailing stop** (`config["trailing_stop"]`, disabled by default) — once
  a position's unrealized gain reaches `activation_pct`, its effective stop
  starts trailing `atr_multiplier` x ATR behind the running high, ratcheting
  up only. A backtest-only research toggle; the live resting broker stop
  always stays fixed at the entry-time ATR stop regardless of this setting.
- **Earnings avoidance** — mirrors the live `catalysts` config exactly (see
  above), so a backtest with `earnings_report_dates` supplied is a faithful
  simulation of what the live earnings-avoidance behavior would have done.

Fill model is deliberately simple (v1): perfect fills at the signal bar's
close for entries and signal exits, exact stop price for a stop-loss/trailing
exit — no slippage or fees modeled, so results read slightly optimistic
versus live trading. Getting real ATR series or earnings data for a batch of
symbols means a lot of single-symbol Robinhood MCP calls — for anything
beyond a handful of symbols, consider delegating that fetch-and-transcribe
work to a background agent rather than doing it inline.

## Testing

```bash
pytest tests/ -q
```

Everything in the suite is deterministic and network-free — the only real
network call in the whole repo is `lib/alerts.py`'s webhook POST, and it's
mocked (`requests.post`) in every test.

## Cloud routines & state persistence

The two skills above are designed to run as **scheduled cloud agents**
(Anthropic's `RemoteTrigger`/routines mechanism, via the `schedule` skill) —
not a machine that has to stay on. This matters because a cloud routine
starts from a **fresh, isolated git clone of this repo on every single run**:
it has no memory of the previous run and no access to your Mac's filesystem
or your local Claude Code session. Three consequences, and how each is
handled:

1. **State needs to survive between runs somewhere other than a gitignored
   local folder.** `data/` (`positions.json`, `candidates.json`,
   `finviz_export.csv`, `logs/`) is gitignored on `main` and stays that way —
   it's never committed to the code-history branch. Instead it lives on a
   dedicated **`bot-state`** branch, kept deliberately separate from `main` so
   the code history isn't cluttered with dozens of small hourly data commits.
   `scripts/sync_state.sh` moves `data/` in and out of that branch:
   - `sync_state.sh pull` — refreshes local `data/` from `origin/bot-state`. Every skill run does this first.
   - `sync_state.sh push` — commits current `data/` back to `origin/bot-state`. Every skill run does this last.
   - Implemented via low-level git plumbing (`write-tree`/`ls-tree`/`mktree`/`commit-tree`) — it never checks out `bot-state`, never switches `HEAD`, and never touches `main`'s commit history. Safe to run from any branch, including mid-run on a cloud agent's checkout of `main`.
   - No-ops cleanly if `data/` hasn't actually changed since the last push.
   - **Concurrency-safe (2026-08-04)**: if two routine runs' pull→modify→push windows overlap (a manual trigger landing near a scheduled one, say), `push()` doesn't just blindly overwrite — a naive "re-fetch and push" is provably unsafe (verified experimentally: it can succeed as a clean fast-forward while silently reverting the other run's changes, since git only checks ref ancestry, not tree content). `pull()` records the exact commit it fetched (`.bot_state_base`, gitignored); `push()` always compares that recorded base against the *current* `origin/bot-state` tip and, if something else landed in between, does a real 3-way merge (`git merge-tree --write-tree`) before pushing — which is why `positions.json`/`trade_history.json`/`candidates.json` are all written pretty-printed with sorted keys (`lib/state.py`), not minified: git's merge is line-based, and one-key-per-line JSON is what lets two runs' disjoint edits (different symbols) merge cleanly instead of looking like a same-line conflict. A genuine conflict — both runs changed the *same* key differently — is never auto-resolved; `push()` fails loudly (non-zero exit) instead of guessing, and both skill docs alert (`state_sync_conflict`) rather than silently moving on.
   - Both skill docs (`.claude/skills/daily-universe-screen.md`,
     `.claude/skills/hourly-signal-check.md`) call this at the start and end
     of every run — this is not optional, it's a mandatory first/last step in
     each runbook.

2. **The Finviz CSV needs a delivery path from your Mac to the cloud.** You
   still export it by hand in the Finviz Elite UI (see above), but instead of
   just saving it to `data/finviz_export.csv` locally, run
   `scripts/publish_finviz_export.sh /path/to/export.csv` — it pulls the
   latest `bot-state` (so it doesn't clobber cloud-accumulated
   positions/candidates state with a stale local copy), copies your export
   in, and pushes `bot-state` back out. Do this before the next scheduled
   daily-screen run needs it.

3. **This repo needs to actually be on GitHub** so a cloud routine has
   something to clone: `git@github.com:dustin-swift/stochastic-cross-trader.git`
   (`main` = code, `bot-state` = data — see above). And the **Robinhood MCP
   connector needs to be explicitly authorized for routine/cloud use** at
   https://claude.ai/customize/connectors — the connector authorized for a
   local Claude Code CLI session is a separate authorization and is not
   automatically available to a cloud routine.

An unattended trading bot that silently loses track of its own open
positions between runs — thinking a slot is open when it's actually
occupied, or vice versa — is a much worse failure mode than anything else
this system guards against, which is why this got built out as real
architecture (a branch + a script + mandatory skill steps) rather than an
assumption that `data/` would "just be there."

## Scheduling

Two scheduled cloud routines, one per skill, created via the `schedule`
skill / `RemoteTrigger`, each pointed at this repo and configured with the
Robinhood MCP connector (see prerequisite #3 above):

- **daily-universe-screen** — once per day, before market open. Requires a
  freshly published Finviz export (`scripts/publish_finviz_export.sh`,
  run locally by you) beforehand.
- **hourly-signal-check** — hourly during market hours, timed to fire just
  after each hourly bar closes (`:02` past the hour, 2026-08-04 — shifted
  from `:30` after a live incident on CUZ where the extra ~30 minutes of
  lag between "bar closes" and "order fires" let a marginal, barely-over-
  the-line confirmation reverse before the trade landed; see
  `scripts/check_hourly_signals.py`'s "Execution-lag cron fix" docstring
  note for the full writeup).
- **daily-stochastic-check** — once per day, after market close. The daily
  paper-comparison track — see "Daily comparison track" below.
- **dashboard-refresh** — hourly during market hours, a few minutes after
  `hourly-signal-check` — see Dashboard below.

Minimum cron interval for a routine is 1 hour. All three routines are safe
to also trigger manually (`RemoteTrigger` `action: "run"`, or just asking an
agent to follow the skill directly) any time, in addition to their schedule.

## Daily comparison track (config/strategy_daily.yaml, 2026-08-13)

A second, parallel strategy track built to directly answer "would this do
better on daily bars instead of hourly?" — same stochastic entry/exit rules,
same $100/15-slot sizing, and (at the user's explicit choice) the same
daily-screened candidate list as the hourly track above, but every
oscillator/ATR/trend/market-regime reading is computed on DAILY bars
instead of hourly ones. See `.claude/skills/daily-stochastic-check.md` for
the full runbook and `config/strategy_daily.yaml` for the rationale behind
every value that differs from the hourly config (mainly: SMA periods that
made sense at hourly granularity don't automatically make sense at daily
granularity, so several were re-derived rather than copied as-is).

**Permanent paper/dry-run — never trades real money.** This was an explicit
choice (asked directly, 2026-08-13: dry-run-only vs. live-with-split-capital
— dry-run won, to keep the comparison clean and avoid the two tracks
competing for capital/slots in the same account). `config/strategy_daily.yaml`
has `live: false`, but that's not the only safeguard — the
`daily-stochastic-check` skill is written to never call an order-placing
tool under any circumstance, full stop. Every entry/exit on this track is a
**simulated fill** recorded straight into `data/daily/positions.json` /
`data/daily/trade_history.json` — no broker order, no broker fill, nothing
to reconcile. The stop-loss itself is simulated too (`scripts/
check_paper_stops.py`, run each cycle before the ordinary signal-exit check):
since there's no real resting stop order to check for a fill, this instead
applies the same fill convention `lib/backtest.py` uses for historical
research — a stop "fills" at the exact stop price on the first bar whose low
touches or breaches it.

**Separate state, shared candidates.** This track's positions, pending
dual-cross setups, trade history, and last-cycle timestamp all live under
`data/daily/` — completely separate from the hourly (live) track's
`data/*.json`, so the two can never collide, double-count, or accidentally
compete for the same simulated/real slot. The one deliberate exception:
`data/candidates.json` (the daily-universe-screen's output) is read
directly by both tracks, not duplicated — `scripts/build_entries_payload.py`'s
`--candidates-data-dir` flag is what makes that split possible (positions/
pending come from `--data-dir data/daily`, candidates come from
`--candidates-data-dir data`).

**Comparing results**: the dashboard's "Daily · Paper" tab (see Dashboard
below) shows this track's open positions and closed-trade performance
side by side with the hourly track's real results — same layout, same
metrics, so a win-rate/P&L comparison is a glance, not a spreadsheet
exercise.

## Dashboard

A published Claude Artifact — positions, closed-trade history with
drill-down, today's watchlist, and portfolio/system stats — kept fresh by
the **dashboard-refresh** cloud routine on the same hourly cadence as
trading itself, with no manual "refresh" step required. It has two tabs:
**Hourly · Live** (the real-money track described above) and
**Daily · Paper** (the comparison track described in "Daily comparison
track" above, clearly banner-labeled as paper/dry-run).

- `dashboard/template.html` — the page itself (self-contained: fonts
  embedded as base64 `@font-face` data URIs, since a published Artifact's
  strict CSP blocks any outbound font/CDN/API request at view time).
- `dashboard/fonts/*.b64` — the embedded IBM Plex Sans / JetBrains Mono
  variable-font files, base64-encoded.
- `scripts/build_dashboard.py` — renders `template.html` into
  `dashboard/dist.html`, pulling positions/trade history/candidates/logs/
  config straight from `data/` and `config/strategy.yaml` for the Hourly
  tab, and from `data/daily/` and `config/strategy_daily.yaml` (via
  `--daily-data-dir`/`--daily-config`, defaulted to those paths) for the
  Daily tab — degrades to an empty Daily tab rather than failing if that
  track hasn't run yet. The only inputs it can't derive itself — live
  account totals and current prices for open positions, hourly track only,
  since the daily track never touches the real broker — come in via a small
  JSON payload on stdin (see the script's own docstring for the exact shape).
- The **dashboard-refresh** routine's job each run: `sync_state.sh pull`,
  fetch that live snapshot via the Robinhood MCP connector, run
  `build_dashboard.py`, then publish `dashboard/dist.html` with the
  Artifact tool using the dashboard's existing URL (so it updates in place
  rather than minting a new one each time).

Since a published Artifact page runs in a locked-down sandbox with no
general network access, this "rebuild and republish" pattern — rather than
having the page fetch its own data live — is what makes hourly refresh
possible without a connected GitHub connector. If you ever need it current
sooner than the next scheduled run, just ask an agent to run the
`dashboard-refresh` routine, or follow its steps manually.

## Safety notes

- The daily-loss circuit breaker (`risk.max_daily_loss_pct` in config) stops
  new entries for the day if realized+unrealized P&L drops past the
  threshold. It does not close existing positions — those still exit via
  their resting stop or a signal-based exit (ordinary crossover, or the
  overbought-hold reversal — see Exit logic above). It also trips (blocks new
  entries) if account value reads as $0 — e.g. before the account is funded.
- If a resting stop order ever fails to place after a live entry fill, that's
  logged as a `stop_placement_failed` event **and sent as an alert** — it
  needs a human to look at it immediately.
