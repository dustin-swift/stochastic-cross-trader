import numpy as np
import pandas as pd
import pytest

from lib.signals import (
    STATE_NORMAL,
    STATE_OVERBOUGHT_HOLD,
    advance_pending_entry,
    exit_signal,
    stop_price,
    update_stochastic_state,
)


def _stoch_df(k_values, d_values):
    return pd.DataFrame({"k": k_values, "d": d_values})


def test_advance_pending_entry_starts_pending_when_k_crosses_but_d_hasnt():
    # prior bar k=18 (<20, satisfies "was below 20"); signal bar k=22 crosses
    # above 20, but d=15 hasn't -- becomes pending, no entry yet.
    df = _stoch_df([30, 18, 22], [35, 19, 15])
    result = advance_pending_entry(None, df)
    assert result["signal"] is False
    assert result["pending"] == {"k_at_cross": 22.0}


def test_advance_pending_entry_fires_when_both_cross_on_the_same_bar():
    # prior bar k=18,d=19 (both <20); signal bar k=22,d=21 (both >=20), k<=55.
    df = _stoch_df([30, 18, 22], [35, 19, 21])
    result = advance_pending_entry(None, df)
    assert result["signal"] is True
    assert result["pending"] is None


def test_advance_pending_entry_fires_on_a_later_bar_once_d_catches_up():
    # Simulates two separate hourly cycles: first call starts pending
    # (k crosses, d doesn't), second call passes a new bar where d finally
    # crosses too, carrying the pending state returned by the first call.
    df1 = _stoch_df([30, 18, 22], [35, 19, 15])
    r1 = advance_pending_entry(None, df1)
    assert r1["pending"] == {"k_at_cross": 22.0}

    df2 = _stoch_df([18, 22, 24], [19, 15, 21])  # next bar: k=24, d=21
    r2 = advance_pending_entry(r1["pending"], df2)
    assert r2["signal"] is True
    assert r2["pending"] is None


def test_advance_pending_entry_invalidates_when_k_drops_back_below_20():
    pending = {"k_at_cross": 22.0}
    # k has fallen back under 20 while still pending -- invalidated regardless of d.
    df = _stoch_df([22, 18], [21, 25])
    result = advance_pending_entry(pending, df)
    assert result["signal"] is False
    assert result["pending"] is None


def test_advance_pending_entry_invalidates_when_k_too_extended_at_d_confirm():
    pending = {"k_at_cross": 22.0}
    # d finally crosses above 20, but k has run to 60 -- past k_invalidate_max=55.
    df = _stoch_df([40, 60], [18, 21])
    result = advance_pending_entry(pending, df, k_invalidate_max=55)
    assert result["signal"] is False
    assert result["pending"] is None


def test_advance_pending_entry_fires_at_exact_k_invalidate_max_boundary():
    pending = {"k_at_cross": 22.0}
    df = _stoch_df([40, 55], [18, 21])
    result = advance_pending_entry(pending, df, k_invalidate_max=55)
    assert result["signal"] is True
    assert result["pending"] is None


def test_advance_pending_entry_stays_pending_while_neither_condition_fires():
    pending = {"k_at_cross": 22.0}
    df = _stoch_df([22, 25], [15, 17])  # k still >=20, d still <20
    result = advance_pending_entry(pending, df)
    assert result["signal"] is False
    assert result["pending"] == pending


def test_advance_pending_entry_false_no_threshold_crossover():
    df = _stoch_df([30, 25, 30], [35, 26, 28])
    result = advance_pending_entry(None, df)
    assert result["signal"] is False
    assert result["pending"] is None


def test_advance_pending_entry_false_insufficient_history():
    df = _stoch_df([22], [20])
    result = advance_pending_entry(None, df)
    assert result["signal"] is False
    assert result["pending"] is None


def test_advance_pending_entry_false_with_nan():
    # NaN in the immediately-preceding bar (the one actually evaluated for
    # default lookback_bars=1) must short-circuit, not raise/compare NaN.
    df = _stoch_df([30, np.nan, 22], [35, np.nan, 20])
    result = advance_pending_entry(None, df)
    assert result["signal"] is False
    assert result["pending"] is None


def test_advance_pending_entry_false_with_nan_outside_lookback_window():
    # NaN further back than the lookback window doesn't matter.
    df = _stoch_df([np.nan, 18, 22], [np.nan, 19, 15])
    result = advance_pending_entry(None, df)
    assert result["pending"] == {"k_at_cross": 22.0}


def test_advance_pending_entry_wider_lookback_still_starts_pending():
    df = _stoch_df([30, 25, 18, 22], [35, 22, 19, 15])
    result = advance_pending_entry(None, df, lookback_bars=3)
    assert result["pending"] == {"k_at_cross": 22.0}


def test_exit_signal_true_bearish_crossover():
    df = _stoch_df([25, 18], [20, 19])
    assert exit_signal(df) is True


def test_exit_signal_false_already_below():
    df = _stoch_df([15, 10], [20, 18])
    assert exit_signal(df) is False


def test_exit_signal_false_with_nan():
    df = _stoch_df([25, np.nan], [20, 19])
    assert exit_signal(df) is False


def test_exit_signal_default_state_is_normal():
    # Explicit STATE_NORMAL matches the default (no state param) — same
    # bearish-crossover behavior either way.
    df = _stoch_df([25, 18], [20, 19])
    assert exit_signal(df) == exit_signal(df, state=STATE_NORMAL)


# -- trend-intact filter (spec §4a) ---------------------------------------


def test_exit_signal_trend_intact_suppresses_bearish_crossover():
    # Same genuine bearish crossover as test_exit_signal_true_bearish_crossover
    # (which asserts True with no trend_intact arg) -- trend_intact=True
    # suppresses it entirely.
    df = _stoch_df([25, 18], [20, 19])
    assert exit_signal(df, trend_intact=True) is False


def test_exit_signal_trend_broken_allows_bearish_crossover():
    df = _stoch_df([25, 18], [20, 19])
    assert exit_signal(df, trend_intact=False) is True


def test_exit_signal_trend_intact_none_falls_through_to_normal():
    # Default / "couldn't determine trend" must never silently trap a
    # position open -- same behavior as omitting the argument entirely.
    df = _stoch_df([25, 18], [20, 19])
    assert exit_signal(df, trend_intact=None) is True
    assert exit_signal(df, trend_intact=None) == exit_signal(df)


def test_exit_signal_trend_intact_does_not_create_false_exit():
    # trend_intact should never itself trigger an exit when there's no
    # crossover to begin with -- it only ever suppresses, never fires.
    df = _stoch_df([15, 10], [20, 18])
    assert exit_signal(df, trend_intact=False) is False
    assert exit_signal(df, trend_intact=True) is False


def test_exit_signal_trend_intact_has_no_effect_on_overbought_hold():
    # OVERBOUGHT_HOLD is governed purely by the downside-80 rule --
    # trend_intact must not change that branch's behavior at all, in either
    # direction.
    still_overbought = _stoch_df([90, 85], [88, 84])
    dropped_below = _stoch_df([90, 70], [88, 75])
    for trend in (True, False, None):
        assert exit_signal(still_overbought, state=STATE_OVERBOUGHT_HOLD, trend_intact=trend) is False
        assert exit_signal(dropped_below, state=STATE_OVERBOUGHT_HOLD, trend_intact=trend) is True


# -- overbought-hold state machine (spec §4 refinement) ------------------


def test_update_state_stays_normal_when_never_overbought():
    df = _stoch_df([30, 45, 60], [28, 40, 55])
    assert update_stochastic_state(STATE_NORMAL, df) == STATE_NORMAL


def test_update_state_transitions_when_both_lines_at_or_above_threshold():
    df = _stoch_df([60, 82], [58, 81])
    assert update_stochastic_state(STATE_NORMAL, df, overbought_threshold=80) == STATE_OVERBOUGHT_HOLD


def test_update_state_transitions_at_exact_threshold():
    df = _stoch_df([60, 80], [58, 80])
    assert update_stochastic_state(STATE_NORMAL, df, overbought_threshold=80) == STATE_OVERBOUGHT_HOLD


def test_update_state_does_not_transition_when_only_k_spikes():
    # %K pokes above 80 but %D hasn't followed -> must NOT enter overbought-hold.
    df = _stoch_df([60, 85], [58, 65])
    assert update_stochastic_state(STATE_NORMAL, df, overbought_threshold=80) == STATE_NORMAL


def test_update_state_does_not_transition_when_only_d_is_high():
    df = _stoch_df([60, 70], [58, 85])
    assert update_stochastic_state(STATE_NORMAL, df, overbought_threshold=80) == STATE_NORMAL


def test_update_state_is_sticky_even_if_lines_have_since_dropped():
    # Already in the hold state -> stays there even though the latest bar's
    # values alone wouldn't newly trigger it; only exit_signal leaves the hold.
    df = _stoch_df([60, 50], [58, 45])
    assert update_stochastic_state(STATE_OVERBOUGHT_HOLD, df) == STATE_OVERBOUGHT_HOLD


def test_update_state_missing_field_defaults_to_normal_behavior():
    # A position written before this feature existed has no stochastic_state
    # field; any non-hold value must behave exactly like STATE_NORMAL.
    df = _stoch_df([60, 82], [58, 81])
    assert update_stochastic_state("", df, overbought_threshold=80) == STATE_OVERBOUGHT_HOLD


def test_update_state_nan_does_not_transition():
    df = _stoch_df([60, np.nan], [58, np.nan])
    assert update_stochastic_state(STATE_NORMAL, df, overbought_threshold=80) == STATE_NORMAL


def test_overbought_hold_ignores_whipsaw_crossovers():
    # Spec's example: both lines pinned near 100, crossing above/below each
    # other repeatedly while price keeps climbing -> must not exit on any of it.
    whipsaw_k = [95, 99, 88, 97, 91, 98]
    whipsaw_d = [93, 90, 96, 89, 97, 90]
    for i in range(2, len(whipsaw_k) + 1):
        df = _stoch_df(whipsaw_k[:i], whipsaw_d[:i])
        assert exit_signal(df, state=STATE_OVERBOUGHT_HOLD, overbought_threshold=80) is False


def test_overbought_hold_exits_when_both_lines_drop_below_threshold():
    df = _stoch_df([95, 78], [93, 76])
    assert exit_signal(df, state=STATE_OVERBOUGHT_HOLD, overbought_threshold=80) is True


def test_overbought_hold_does_not_exit_if_only_one_line_drops():
    # %K dips below 80 but %D is still >= 80 -> still holding.
    df = _stoch_df([95, 75], [93, 85])
    assert exit_signal(df, state=STATE_OVERBOUGHT_HOLD, overbought_threshold=80) is False


def test_overbought_hold_exit_ignores_stale_nan_history():
    # Only the latest bar matters for the hold-exit check, not history shape.
    df = _stoch_df([np.nan, 95, 75], [np.nan, 93, 76])
    assert exit_signal(df, state=STATE_OVERBOUGHT_HOLD, overbought_threshold=80) is True


def test_overbought_hold_exit_false_when_latest_bar_is_nan():
    df = _stoch_df([95, np.nan], [93, np.nan])
    assert exit_signal(df, state=STATE_OVERBOUGHT_HOLD, overbought_threshold=80) is False


def test_full_lifecycle_normal_then_hold_then_exit():
    # Simulates the state carrying across cycles: NORMAL entry that never
    # reaches overbought exits on an ordinary bearish cross (unchanged
    # behavior); a separate run that does reach overbought whipsaws freely
    # then exits only on the double drop below threshold.

    # -- unchanged path: ordinary bearish crossover, never overbought -----
    state = STATE_NORMAL
    df = _stoch_df([40, 55, 45], [38, 50, 52])  # crosses below on the last bar
    state = update_stochastic_state(state, df, overbought_threshold=80)
    assert state == STATE_NORMAL
    assert exit_signal(df, state=state, overbought_threshold=80) is True

    # -- overbought-hold path ------------------------------------------------
    state = STATE_NORMAL
    bars_k = [40, 60, 82, 91, 88, 95, 78]
    bars_d = [38, 55, 81, 95, 90, 85, 76]
    expected_states = [
        STATE_NORMAL,  # after bar 2 (index1)
        STATE_OVERBOUGHT_HOLD,  # after bar 3 (index2) -> both >= 80
        STATE_OVERBOUGHT_HOLD,
        STATE_OVERBOUGHT_HOLD,
        STATE_OVERBOUGHT_HOLD,
        STATE_OVERBOUGHT_HOLD,
    ]
    exited = False
    for i in range(2, len(bars_k) + 1):
        df = _stoch_df(bars_k[:i], bars_d[:i])
        state = update_stochastic_state(state, df, overbought_threshold=80)
        assert state == expected_states[i - 2]
        signal = exit_signal(df, state=state, overbought_threshold=80)
        if i < len(bars_k):
            assert signal is False
        else:
            exited = signal
    assert exited is True  # final bar (78/76, both < 80) triggers the exit


def test_stop_price_basic():
    assert stop_price(100.0, 2.0, mult=1.5) == pytest.approx(97.0)


def test_stop_price_default_mult():
    assert stop_price(50.0, 1.0) == pytest.approx(48.5)


@pytest.mark.parametrize("entry_price", [0, -5])
def test_stop_price_invalid_entry_price(entry_price):
    with pytest.raises(ValueError):
        stop_price(entry_price, 1.0)


@pytest.mark.parametrize("atr", [-1, float("nan")])
def test_stop_price_invalid_atr(atr):
    with pytest.raises(ValueError):
        stop_price(100.0, atr)
