"""Entry/exit signal logic (spec sections 3-4). Pure functions over an
already-computed stochastic DataFrame (see lib.indicators.stochastic) — no
network calls. The "signal bar" is always the last row of the DataFrame.

Note on spec section 3's condition 1 ("%K was below 20 on a prior bar"): a
literal "%K crosses above 20" already implies the immediately-preceding bar
was below 20, which would make that condition redundant. We keep it as an
explicit, separately-checked condition (`lookback_bars`, default 1 = just the
immediately-preceding bar) so it's harmless when redundant, but can be widened
to look further back (a genuine oversold dip a few bars ago, not just brushing
the line) via config without a code change if that's the intended reading.

Exit logic is a small per-position state machine, not a single stateless
check (spec §4 overbought-hold refinement): once %K and %D both push above
`overbought_threshold`, minor whipsaws between the two lines up there don't
mean the move is reversing — a stock can spend a long time pinned near 100
while price keeps climbing (the position's `stochastic_state` is what makes
that history visible to this bar's check; without it, every stateless
crossover check would look identical whether the stock just left oversold or
has been running hot for days). The two states are:
- STATE_NORMAL (default on entry): exit on an ordinary bearish %K/%D crossover.
- STATE_OVERBOUGHT_HOLD (entered once both lines are >= threshold together):
  ignore all crossovers; exit only once both lines drop back below the
  threshold. Callers are responsible for persisting `stochastic_state` on the
  position between cycles (see StateStore) and calling
  `update_stochastic_state` before `exit_signal` each cycle.

Trend-intact filter (spec §4a, 2026-07-30): a generalization of
overbought-hold below the 80 line. Stochastic is well known to whipsaw
repeatedly during a genuinely trending move — a bearish %K/%D crossover
mid-trend doesn't mean the trend has broken, just that momentum paused for a
bar or two. `exit_signal`'s STATE_NORMAL branch takes an optional
`trend_intact` argument (computed by the caller from price structure, e.g.
close vs. a rising SMA — see lib.indicators.sma): when True, the crossover
exit is suppressed regardless of what %K/%D just did, the same way
STATE_OVERBOUGHT_HOLD suppresses it above 80. This is stateless (recomputed
every cycle from current price/SMA, not tracked on the position) and only
gates the STATE_NORMAL branch — STATE_OVERBOUGHT_HOLD's behavior is
completely unaffected. The hard ATR stop is unaffected by either mechanism.
"""
from __future__ import annotations

import math

import pandas as pd

STATE_NORMAL = "NORMAL"
STATE_OVERBOUGHT_HOLD = "OVERBOUGHT_HOLD"


def _last_two_valid(stoch_df: pd.DataFrame, lookback_bars: int) -> bool:
    """True if stoch_df has enough non-NaN history to evaluate a signal on the
    last bar: the last bar's k/d, the previous bar's k/d, and `lookback_bars`
    of prior k values must all be present.
    """
    if len(stoch_df) < 2:
        return False
    window = stoch_df.iloc[-(lookback_bars + 1) :]
    if window[["k"]].isna().any().any():
        return False
    if stoch_df["d"].iloc[-2:].isna().any():
        return False
    return True


def entry_signal(stoch_df: pd.DataFrame, oversold_threshold: float = 20, lookback_bars: int = 1) -> bool:
    """True iff, on the last (signal) bar:
    - %K crosses above `oversold_threshold` (prev %K < threshold <= current %K)
    - %K crosses above %D in the same bar (prev %K <= prev %D, current %K > current %D)
    - %K was below `oversold_threshold` at some point in the `lookback_bars`
      bars strictly before the signal bar (spec §3 condition 1).
    """
    if not _last_two_valid(stoch_df, lookback_bars):
        return False

    k = stoch_df["k"]
    d = stoch_df["d"]

    prev_k, cur_k = k.iloc[-2], k.iloc[-1]
    prev_d, cur_d = d.iloc[-2], d.iloc[-1]

    crossed_above_threshold = prev_k < oversold_threshold <= cur_k
    crossed_above_d = prev_k <= prev_d and cur_k > cur_d

    lookback_window = k.iloc[-(lookback_bars + 1) : -1]
    was_oversold_prior = (lookback_window < oversold_threshold).any()

    return bool(crossed_above_threshold and crossed_above_d and was_oversold_prior)


def _latest_k_d(stoch_df: pd.DataFrame) -> tuple[float, float] | None:
    """Latest (k, d) pair, or None if there's no bar or either value is NaN."""
    if len(stoch_df) < 1:
        return None
    k, d = stoch_df["k"].iloc[-1], stoch_df["d"].iloc[-1]
    if pd.isna(k) or pd.isna(d):
        return None
    return k, d


def update_stochastic_state(
    state: str, stoch_df: pd.DataFrame, overbought_threshold: float = 80
) -> str:
    """Advance a position's `stochastic_state` machine by one bar (spec §4
    overbought-hold refinement). Call this BEFORE `exit_signal` each cycle —
    `exit_signal` needs the state as of *this* bar, including a transition
    that just happened on this same bar (e.g. a spike straight to 100 should
    already suppress a same-bar crossover exit, not wait a cycle).

    STATE_OVERBOUGHT_HOLD is sticky: once entered, this function never moves
    a position back to STATE_NORMAL — leaving the hold state only happens via
    the exit itself (both lines dropping back below the threshold), which
    closes the position, not via this state update.

    Any input state other than STATE_OVERBOUGHT_HOLD is treated as
    STATE_NORMAL, so a position missing the field (e.g. written before this
    feature existed) behaves correctly without a migration.
    """
    if state == STATE_OVERBOUGHT_HOLD:
        return STATE_OVERBOUGHT_HOLD

    latest = _latest_k_d(stoch_df)
    if latest is None:
        return STATE_NORMAL

    k, d = latest
    if k >= overbought_threshold and d >= overbought_threshold:
        return STATE_OVERBOUGHT_HOLD
    return STATE_NORMAL


def exit_signal(
    stoch_df: pd.DataFrame,
    state: str = STATE_NORMAL,
    overbought_threshold: float = 80,
    trend_intact: bool | None = None,
) -> bool:
    """Exit trigger for the current bar, given the position's (already
    updated — see `update_stochastic_state`) `stochastic_state`:

    - STATE_NORMAL: True iff %K crosses below %D on the last bar (bearish
      crossover): prev %K >= prev %D, current %K < current %D — UNLESS
      `trend_intact` is True, in which case the crossover is suppressed
      entirely (spec §4a trend-intact filter, see below).
    - STATE_OVERBOUGHT_HOLD: crossovers are ignored entirely (a stock can
      whipsaw %K/%D against each other many times while pinned near 100 and
      still be working — see spec §4). True only when both %K and %D are
      currently below `overbought_threshold`. `trend_intact` has NO effect
      here — this branch is completely unchanged by the trend filter.

    Any state other than STATE_OVERBOUGHT_HOLD is treated as STATE_NORMAL.

    `trend_intact` (spec §4a, generalizes overbought-hold below the 80 line):
    stochastic is a mean-reversion/momentum oscillator and is well known to
    whipsaw repeatedly during a strongly trending move — %K/%D can cross
    bearishly several times mid-trend without the trend itself having broken.
    The caller determines "is the trend still intact" from price structure
    (e.g. close still above a rising SMA) and passes the answer in here; this
    function only applies the resulting gate, it doesn't compute the trend
    itself (see lib.indicators.sma). `trend_intact=None` (the default; also
    used when the caller couldn't determine it, e.g. insufficient bars for
    the SMA) falls through to the plain crossover check — missing trend data
    must never silently trap a position open forever, only *known-intact*
    trend suppresses the exit.
    """
    if state == STATE_OVERBOUGHT_HOLD:
        latest = _latest_k_d(stoch_df)
        if latest is None:
            return False
        k, d = latest
        return bool(k < overbought_threshold and d < overbought_threshold)

    if len(stoch_df) < 2:
        return False
    k = stoch_df["k"]
    d = stoch_df["d"]
    if k.iloc[-2:].isna().any() or d.iloc[-2:].isna().any():
        return False

    prev_k, cur_k = k.iloc[-2], k.iloc[-1]
    prev_d, cur_d = d.iloc[-2], d.iloc[-1]

    crossed_below = bool(prev_k >= prev_d and cur_k < cur_d)
    if trend_intact:
        return False
    return crossed_below


def stop_price(entry_price: float, atr14: float, mult: float = 1.5) -> float:
    """ATR-based stop-loss level (spec §4): entry_price - mult * ATR(14)."""
    if entry_price <= 0:
        raise ValueError("entry_price must be positive")
    if atr14 < 0 or math.isnan(atr14):
        raise ValueError("atr14 must be a non-negative number")
    return entry_price - mult * atr14
