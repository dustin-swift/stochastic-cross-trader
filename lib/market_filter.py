"""Market-regime filter (2026-08-06, at the user's direction): only open new
positions while the broad market itself isn't in a steep short-term decline.
Pure function over already-fetched bars -- no network calls, mirrors the
shape of lib.catalysts/lib.risk (a single boolean gate the hourly-signal-
check skill checks before evaluating candidates, same pattern as the circuit
breaker).

Why: a live-trade review (2026-08-06) found two positions (CNQ, DINO) enter
within a minute of each other and reverse for a loss within seconds of each
other the next day -- correlated, not independent, failures. The strategy's
per-symbol dual-cross entry has no visibility into whether the broad market
itself is dropping; requiring the market not be in a decline before opening
new risk is meant to reduce exactly that kind of simultaneous, correlated
drawdown.

**Revision history, both same-day (2026-08-06):**
- v1 used HOURLY bars (20/200-period SMAs spanning ~3-4 trading days and
  ~5-7 weeks) with a "fast above slow AND rising" requirement -- a genuine
  regime-level read. The user then pointed out this can't actually catch
  what it was built for: a single steep down day, even a real one, usually
  isn't enough to pull a 3-day average below a 6-week average inside an
  otherwise-intact longer uptrend. A slow filter can answer "is the broad
  market in an uptrend" but structurally cannot answer "is there a steep
  selloff happening right now" -- two different questions, one lookback
  horizon can't serve both.
- v2 (current) switches to MINUTE bars, same 20/200 periods -- now spanning
  roughly 20 minutes and ~3.3 hours, a same-day momentum read rather than a
  multi-week regime read. Also drops the "rising" requirement (now a plain
  fast-above-slow comparison, at the user's direction) since a fast-moving
  minute-level average is noisy enough that a separate slope check adds
  little and risks flipping the gate on ordinary noise. **Known trade-off,
  flagged rather than assumed away**: at this timeframe the filter can also
  trip during an ordinary intraday pullback-and-bounce, not just a genuine
  steep selloff -- exactly the kind of dip this strategy exists to buy into.
  Worth monitoring whether it blocks good entries too often, not just bad
  ones, after some live cycles.

Exits and existing positions are never affected by this filter -- like the
circuit breaker, it only ever gates NEW entries (see the hourly-signal-check
skill, which checks this before section 5, entries, early enough that a
failing check skips the entire candidate fetch for the cycle too).

**Caller must fetch bars with extended-hours bounds (2026-08-19 fix)**: this
function is agnostic to session type, it just averages whatever closes it's
given -- but a caller passing regular-hours-only bars will get a
gap-blind read for any cycle whose fetch lands near the regular-session
open. Confirmed live: a cycle running 12 seconds after the 13:30 UTC open,
on a morning SPY gapped up +0.38% overnight, computed `trend_intact: false`
because zero regular-session bars existed yet -- the 200-period window was
almost entirely the prior session's closes. Feeding this function bars
fetched with `bounds="extended"` instead fixes it: pre-market bars from the
same morning are already available and dense, so the SMA windows reflect
today's actual overnight/pre-market momentum by the time the regular
session opens, rather than needing 20+ minutes of fresh regular-session
bars to catch up from a standing start. See the hourly-signal-check skill's
market-regime-check step and the README's market-regime-filter section.
"""
from __future__ import annotations

import pandas as pd

from lib.indicators import sma


def market_trend_intact(
    bars: list[dict],
    fast_period: int = 20,
    slow_period: int = 200,
    rising_lookback_bars: int = 5,
    require_rising: bool = True,
) -> bool | None:
    """True iff the market (bars for a broad index/ETF, e.g. SPY) is not in
    a decline: the fast SMA is above the slow SMA -- and, when
    `require_rising` is True (the default, matching the original hourly
    design), the fast SMA is also higher now than it was
    `rising_lookback_bars` bars ago (a genuine rise, not just "currently
    above"). `require_rising=False` (used by the current minute-bar
    deployment, see module docstring) drops that second condition entirely
    -- a plain fast-above-slow comparison.

    Returns `None` (not False) when there isn't enough bar history to
    evaluate -- missing/insufficient data must never silently read as
    "market declining" and block every entry; the caller should treat None
    the same as "can't determine, don't block" (see lib.signals.exit_signal's
    trend_intact for the same convention on the exit side).
    """
    df = pd.DataFrame(bars)[["close"]].astype(float)
    min_len = slow_period + (rising_lookback_bars if require_rising else 0)
    if len(df) < min_len:
        return None

    fast = sma(df, period=fast_period, column="close")
    slow = sma(df, period=slow_period, column="close")

    latest_fast, latest_slow = fast.iloc[-1], slow.iloc[-1]
    if pd.isna(latest_fast) or pd.isna(latest_slow):
        return None

    above = latest_fast > latest_slow
    if not require_rising:
        return bool(above)

    prior_fast = fast.iloc[-1 - rising_lookback_bars]
    if pd.isna(prior_fast):
        return None

    rising = latest_fast > prior_fast
    return bool(above and rising)
