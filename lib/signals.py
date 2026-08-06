"""Entry/exit signal logic (spec sections 3-4). Pure functions over an
already-computed stochastic DataFrame (see lib.indicators.stochastic) — no
network calls. The "signal bar" is always the last row of the DataFrame.

Basing-pattern requirement (`lookback_bars`, 2026-08-06 revision, at the
user's direction): a literal "%K crosses above 20" already implies the
immediately-preceding bar was below 20, so a `lookback_bars=1` check is
mostly redundant with the crossing condition itself -- one bar dipping under
20 and immediately bouncing can be ordinary noise in a downtrend, not a real
reversal. `lookback_bars` now requires ALL of the N bars immediately before
the crossing bar (not just one, and not just "any of them") to have been
below `oversold_threshold` -- a genuine multi-bar basing/consolidation under
the line, not a brush past it. This was widened from a v1 that checked "was
it oversold *anywhere* in the lookback window" (`.any()`), which the
2026-08-04 backtest notes correctly flagged as a no-op for lookback_bars > 1
once the crossing condition already guarantees the immediately-preceding bar
qualifies -- `.all()` is what actually makes a wider window stricter.
Confirmed live as a real problem: CUZ/CNQ/DNTH/DINO all confirmed with %D
barely above 20 (21-26 range) off a single-bar dip, several reversing for a
loss within a day. Default is 2 (see config/strategy.yaml) -- one bar of
confirmed basing below the immediately-crossing bar, not just the crossing
bar's own predecessor.

Entry logic (2026-08-04 dual-cross revision, see `advance_pending_entry`):
the original rule fired the instant %K crossed above 20 and above %D in the
same bar, with no requirement on %D's own level — meaning %D, a slower
3-period average of %K, was very often still below 20 at the exact moment of
entry (confirmed live: LEVI entered with %K=20.52 but %D=15.53). At the
user's direction, entries now require BOTH %K and %D to cross above 20
before firing, since %D crossing is what distinguishes a genuine, sustained
momentum reversal from a brief %K spike. Because %D lags, this can't be a
single-bar check anymore: %K crossing 20 starts a *pending* state that
persists across hourly cycles (see `lib.state`'s pending-entries store) until
%D also crosses — with a ceiling (`k_invalidate_max`, default 55) on how far
%K is allowed to have run by the time %D confirms, since a %D confirmation
arriving long after %K has already pushed well past the oversold zone isn't
really confirming a *reversal* anymore, it's just lagging a move that's
already happened. The original "%K crosses %D in the same bar" condition is
dropped entirely — both lines independently clearing 20 is the new
confirmation.

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


def advance_pending_entry(
    pending: dict | None,
    stoch_df: pd.DataFrame,
    oversold_threshold: float = 20,
    k_invalidate_max: float = 55,
    lookback_bars: int = 1,
) -> dict:
    """Advance a candidate's pending-entry state by one bar (spec §3,
    2026-08-04 dual-cross revision — see module docstring for why this
    replaced the original single-bar "%K crosses %D" rule). Call this on
    every candidate every cycle, whether or not it's currently pending —
    same calling convention as `update_stochastic_state` for open positions.

    `pending` is `None` if this symbol isn't currently in a pending setup,
    or `{"k_at_cross": float}` if %K has already crossed above
    `oversold_threshold` and the setup is waiting for %D to also cross.

    Returns `{"pending": <new state, None or dict>, "signal": bool}`.
    `signal=True` means BOTH %K and %D have now crossed above
    `oversold_threshold`, with %K at or below `k_invalidate_max` — fire the
    entry. Transitions:

    - Not pending, %K crosses above threshold, AND each of the
      `lookback_bars` bars immediately before the crossing bar was also
      below threshold (basing-pattern requirement, 2026-08-06 -- see module
      docstring) -> becomes pending. If %D has *also* already crossed on
      this same bar, falls through to the pending-evaluation logic below
      immediately rather than forcing an extra bar of waiting.
    - Pending, current %K < threshold -> invalidated (reverts to not
      pending) — the reversal attempt failed, %K never got %D's
      confirmation before losing the level itself. No expiry timer; this is
      the only way a pending setup un-pends without firing.
    - Pending, current %K >= threshold, current %D >= threshold, current
      %K <= `k_invalidate_max` -> entry fires.
    - Pending, current %K >= threshold, current %D >= threshold, current
      %K > `k_invalidate_max` -> invalidated as too-extended: %D confirmed
      too late, %K has already run further than the setup allows for.
    - Pending, current %K >= threshold, current %D < threshold -> stays
      pending, still waiting on %D.
    """
    if not _last_two_valid(stoch_df, lookback_bars):
        return {"pending": pending, "signal": False}

    k = stoch_df["k"]
    d = stoch_df["d"]
    cur_k, cur_d = k.iloc[-1], d.iloc[-1]

    if pending is None:
        prev_k = k.iloc[-2]
        crossed_above_threshold = prev_k < oversold_threshold <= cur_k
        # Basing-pattern requirement (2026-08-06): ALL of the lookback_bars
        # bars immediately before the crossing bar must have been oversold,
        # not just one of them -- see module docstring for why .all() (not
        # .any()) is what makes lookback_bars > 1 an actual stricter check.
        lookback_window = k.iloc[-(lookback_bars + 1) : -1]
        was_oversold_prior = bool((lookback_window < oversold_threshold).all())
        if not (crossed_above_threshold and was_oversold_prior):
            return {"pending": None, "signal": False}
        pending = {"k_at_cross": float(cur_k)}

    if cur_k < oversold_threshold:
        return {"pending": None, "signal": False}

    if cur_d >= oversold_threshold:
        if cur_k <= k_invalidate_max:
            return {"pending": None, "signal": True}
        return {"pending": None, "signal": False}

    return {"pending": pending, "signal": False}


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
