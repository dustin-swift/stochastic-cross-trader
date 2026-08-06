"""Market-regime filter (2026-08-06, at the user's direction): only open new
positions while the broad market itself is trending, not chopping. Pure
function over already-fetched bars -- no network calls, mirrors the shape of
lib.catalysts/lib.risk (a single boolean gate the hourly-signal-check skill
checks before evaluating candidates, same pattern as the circuit breaker).

Why: a live-trade review (2026-08-06) found two positions (CNQ, DINO) enter
within a minute of each other and reverse for a loss within seconds of each
other the next day -- correlated, not independent, failures. The strategy's
per-symbol dual-cross entry has no visibility into whether the broad market
itself is even in a regime where "buy the pullback" tends to work; requiring
the market to already be trending before opening new risk is meant to reduce
exactly that kind of simultaneous, correlated drawdown, and to bias new
entries toward conditions more likely to reach OVERBOUGHT_HOLD (a real winner
worth riding) rather than an immediate signal_exit reversal.

Uses HOURLY bars, not daily (2026-08-06, at the user's direction) -- this
system trades on hourly signals, so the market-regime read should update on
the same cadence, not lag a full day behind. 20/200-period SMAs on hourly
bars span roughly 3-4 trading days and ~5-7 weeks respectively (accounting
for ~6.5 hourly bars per regular session), not the calendar-day spans those
same period counts would imply on a daily chart -- these are deliberately
different indicators from a "20-day/200-day" filter, not a relabeling of one.

Exits and existing positions are never affected by this filter -- like the
circuit breaker, it only ever gates NEW entries (see the hourly-signal-check
skill, which checks this before section 5, entries).
"""
from __future__ import annotations

import pandas as pd

from lib.indicators import sma


def market_trend_intact(
    bars: list[dict],
    fast_period: int = 20,
    slow_period: int = 200,
    rising_lookback_bars: int = 5,
) -> bool | None:
    """True iff the market (bars for a broad index/ETF, e.g. SPY) is in an
    uptrend: the fast SMA is above the slow SMA, AND the fast SMA is higher
    now than it was `rising_lookback_bars` bars ago (a genuine rise, not
    just "currently above" -- a fast SMA that's above the slow SMA but
    flat-to-falling isn't the trending-market condition this filter is
    meant to require).

    Returns `None` (not False) when there isn't enough bar history to
    compute both SMAs plus the rising comparison -- missing/insufficient
    data must never silently read as "market not trending" and block every
    entry; the caller should treat None the same as "can't determine,
    don't block" (see lib.signals.exit_signal's trend_intact for the same
    convention on the exit side).
    """
    df = pd.DataFrame(bars)[["close"]].astype(float)
    if len(df) < slow_period + rising_lookback_bars:
        return None

    fast = sma(df, period=fast_period, column="close")
    slow = sma(df, period=slow_period, column="close")

    latest_fast, latest_slow = fast.iloc[-1], slow.iloc[-1]
    prior_fast = fast.iloc[-1 - rising_lookback_bars]

    if pd.isna(latest_fast) or pd.isna(latest_slow) or pd.isna(prior_fast):
        return None

    above = latest_fast > latest_slow
    rising = latest_fast > prior_fast
    return bool(above and rising)
