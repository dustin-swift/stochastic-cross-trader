# Hourly Stochastic Pullback Trader

Implementation of the strategy in `~/Downloads/hourly-stochastic-strategy-spec (1).md`:
buy fundamentally strong stocks (near 52-week highs, in confirmed uptrends)
during a short-term pullback, entering on an hourly stochastic dual-confirmation
crossover, with an ATR-based resting stop and a state-aware signal exit (see
Exit logic below — extended overbought runs are held through whipsaws instead
of exiting on the first crossover). The daily universe screen is Finviz Elite
(manual export); everything else — hourly bars, ATR, account data, and order
execution — goes through the Robinhood Agentic MCP connector (`.mcp.json`).

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
  cap, entry-order fill/reject/timeout decisions, state (JSON-backed
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
- `data/` — gitignored. `finviz_export.csv` (your manual export — see below),
  `candidates.json` (today's list), `positions.json` (open positions),
  `daily_pnl.json`, `logs/YYYY-MM-DD.jsonl` (every signal check, decision,
  order event, and alert).

## Finviz Elite manual screen

Screening (market cap, price, average volume, % off 52-week high, price vs
SMA50) is **not automated** — you build and export it yourself:

1. In the Finviz Elite screener, set filters:
   - Market Cap: over $2B
   - Price: over $10
   - Average Volume: over 750K
   - 50-Day Simple Moving Average: price above SMA50
   - 52-Week High/Low: within 15% of the 52-week high
2. Export the results to CSV (Elite plans include CSV export).
3. Save the file to `data/finviz_export.csv` (the default path — configurable via `config/strategy.yaml`'s `screening.finviz_csv_path`).
4. Re-export whenever you want to refine the filters, or when the daily screen reports the file is stale.

**Staleness check**: the daily-universe-screen skill refuses to run on a stale
export. "Stale" is trading-day-aware, not just "older than N hours" — a
Friday export is treated as valid through Monday, and goes stale starting
Tuesday. **Known limitation**: this only accounts for weekends, not market
holidays — an export from before a Monday holiday will incorrectly look
fresh on the Tuesday after it. Re-export after any holiday to be safe.

## Running a cycle manually

There's no CLI entry point that does the whole cycle end-to-end by itself —
the Robinhood MCP calls (hourly bars, ATR, order placement) only work from an
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
- `live: true` — the hourly-signal-check skill places real orders: a market
  buy sized in dollars, polled through to a fill/reject/timeout decision
  (see Order-lifecycle robustness below), followed immediately by a real
  resting `stop_market` sell order (not a soft "check next hour" stop). Only
  flip this after you've reviewed dry-run logs and are ready to trade the
  account for real.

To flip live trading on: edit `live: true` in `config/strategy.yaml`. That's
the only change needed — no code changes, no redeploy.

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

## Catalyst avoidance: earnings (config["catalysts"])

The resting protective stop (`stop_market`) can only be placed regular-hours
— a hard Robinhood platform constraint, not a config choice. A pre-market or
after-hours earnings gap gets zero stop protection until regular hours
resume, and the eventual fill can land well below the intended stop. Since
there's no way to protect against the gap itself, the mitigation is avoiding
known earnings dates:

- **Daily screen**: candidates whose next earnings report falls within
  `catalysts.earnings_exclusion_days` (default 5) are excluded from
  `candidates.json` before it's even written. Applied *after* the sector cap,
  not before — earnings-checking the full raw Finviz universe (50-100+ rows)
  would reintroduce the same per-symbol-call scaling problem Finviz replaced
  `create_scan` for in the first place. An excluded slot isn't backfilled
  from the same sector.
- **Hourly check**: open positions are re-checked fresh every cycle (not just
  at entry) — a position can be held for days after the daily screen last
  looked at it and drift into an earnings date that was safely far away at
  entry time. If the next report falls within
  `catalysts.earnings_forced_exit_days` (default 1), the position exits
  immediately (`exit_reason: "earnings_exit"`) regardless of what the
  stochastic oscillator says — this is a scheduled risk decision, not a
  signal read.

Both checks are **optional per call** — a symbol missing `earnings_report_dates`
entirely is handled differently depending on context (see `lib/catalysts.py`
and `scripts/check_hourly_signals.py` docstrings): the daily screen treats
"not checked" conservatively as excluded, but an open position with no
earnings data falls through to normal signal logic rather than being force-
sold — a data-fetch hiccup shouldn't liquidate a live position. If Robinhood
MCP is unavailable, both scripts still work fine with no earnings data at
all, exactly as before this feature existed — it's additive, not required.

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

## Scheduling

Not yet wired up. Per the plan, once manual dry-run cycles have been reviewed,
the next step is two scheduled cloud agents (via the `schedule` skill) running
the two skills above on a daily / hourly cadence — still dry-run first.

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
