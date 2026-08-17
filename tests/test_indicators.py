import math

import numpy as np
import pandas as pd
import pytest

from lib.indicators import atr, rolling_high, sma, stochastic


def _bars(closes, highs=None, lows=None):
    closes = list(closes)
    highs = highs if highs is not None else closes
    lows = lows if lows is not None else closes
    return pd.DataFrame({"high": highs, "low": lows, "close": closes})


def test_stochastic_hand_computed_no_smoothing():
    # k_smooth=1, d_period=1 -> k == raw %K, d == k, so we can hand-verify.
    closes = [10, 11, 12, 13, 14, 12, 11, 10]
    bars = _bars(closes)

    result = stochastic(bars, k_period=3, k_smooth=1, d_period=1)

    assert result["k"].iloc[:2].isna().all()

    expected_k = [100.0, 100.0, 100.0, 0.0, 0.0, 0.0]
    for i, exp in zip(range(2, 8), expected_k):
        assert result["k"].iloc[i] == pytest.approx(exp)
        assert result["d"].iloc[i] == pytest.approx(exp)


def test_stochastic_flat_range_is_nan_not_error():
    # high == low == close for the whole window -> denom is 0, must not raise.
    closes = [10.0] * 10
    bars = _bars(closes)

    result = stochastic(bars, k_period=3, k_smooth=1, d_period=1)

    assert result["k"].iloc[2:].isna().all()
    assert result["d"].iloc[2:].isna().all()


def test_stochastic_default_settings_shape_and_bounds():
    n = 40
    closes = 100 + 5 * np.sin(np.arange(n) / 3.0)
    bars = _bars(closes, highs=closes + 0.5, lows=closes - 0.5)

    result = stochastic(bars, k_period=14, k_smooth=3, d_period=3)

    assert len(result) == n
    # First valid raw %K at index 13 (k_period-1); %K needs 3 consecutive raw
    # values -> first valid %K at index 15; %D needs 3 consecutive %K -> index 17.
    assert result["d"].iloc[:17].isna().all()
    assert result["d"].iloc[17:].notna().all()

    valid = result.dropna()
    assert (valid["k"] >= 0).all() and (valid["k"] <= 100).all()
    assert (valid["d"] >= 0).all() and (valid["d"] <= 100).all()


def test_stochastic_missing_columns_raises():
    bad = pd.DataFrame({"close": [1, 2, 3]})
    with pytest.raises(ValueError):
        stochastic(bad)


def test_sma_hand_computed():
    bars = _bars([10, 20, 30, 40, 50])
    result = sma(bars, period=3)
    assert result.iloc[:2].isna().all()
    assert result.iloc[2] == 20.0  # mean(10,20,30)
    assert result.iloc[3] == 30.0  # mean(20,30,40)
    assert result.iloc[4] == 40.0  # mean(30,40,50)


def test_sma_custom_column():
    bars = pd.DataFrame({"high": [10, 20, 30], "low": [1, 2, 3], "close": [5, 5, 5]})
    result = sma(bars, period=2, column="high")
    assert result.iloc[0] != result.iloc[0]  # NaN
    assert result.iloc[1] == 15.0
    assert result.iloc[2] == 25.0


def test_sma_missing_column_raises():
    bars = pd.DataFrame({"close": [1, 2, 3]})
    with pytest.raises(ValueError):
        sma(bars, period=2, column="high")


# --- atr (MA Pullback / Breakout-Retest agent) ---

def test_atr_hand_computed():
    # true range each bar: max(high-low, |high-prevclose|, |low-prevclose|).
    # Bar 0 has no prior close -> falls back to its own high-low range.
    highs = [12, 13, 14, 12, 11]
    lows = [10, 11, 12, 10, 9]
    closes = [11, 12, 13, 11, 10]
    bars = _bars(closes, highs=highs, lows=lows)

    result = atr(bars, period=2)

    # bar0 TR = 12-10 = 2 (no prior close).
    # bar1 TR = max(13-11=2, |13-11|=2, |11-11|=0) = 2.
    # bar2 TR = max(14-12=2, |14-12|=2, |12-12|=0) = 2.
    # bar3 TR = max(12-10=2, |12-13|=1, |10-13|=3) = 3.
    # bar4 TR = max(11-9=2, |11-11|=0, |9-11|=2) = 2.
    assert result.iloc[0] != result.iloc[0]  # NaN (period=2 needs 2 bars)
    assert result.iloc[1] == pytest.approx((2 + 2) / 2)
    assert result.iloc[2] == pytest.approx((2 + 2) / 2)
    assert result.iloc[3] == pytest.approx((2 + 3) / 2)
    assert result.iloc[4] == pytest.approx((3 + 2) / 2)


def test_atr_missing_columns_raises():
    bad = pd.DataFrame({"close": [1, 2, 3]})
    with pytest.raises(ValueError):
        atr(bad)


def test_atr_default_period_shape():
    n = 30
    closes = 100 + np.cumsum(np.random.default_rng(0).normal(0, 1, n))
    bars = _bars(closes, highs=closes + 1, lows=closes - 1)

    result = atr(bars, period=14)

    assert len(result) == n
    assert result.iloc[:13].isna().all()
    assert result.iloc[13:].notna().all()
    assert (result.dropna() >= 0).all()


# --- rolling_high (MA Pullback / Breakout-Retest agent's breakout level) ---

def test_rolling_high_include_today_false_excludes_current_bar():
    # The core spec correction: breakout_level must be the PRIOR lookback-bar
    # high, never today's own (possibly higher) high.
    highs = [10, 11, 12, 30]  # last bar spikes far above everything before it
    bars = pd.DataFrame({"high": highs})

    result = rolling_high(bars, lookback=3, column="high", include_today=False)

    # Row 3 (the spike) must report the highest of the PRIOR 3 bars (12), not
    # its own value (30).
    assert result.iloc[3] == 12
    assert result.iloc[:3].isna().all()  # not enough prior history for rows 0-2


def test_rolling_high_include_today_true_is_ordinary_trailing_max():
    highs = [10, 11, 12, 30]
    bars = pd.DataFrame({"high": highs})

    result = rolling_high(bars, lookback=3, column="high", include_today=True)

    assert result.iloc[3] == 30  # includes today's own spike this time
    assert result.iloc[2] == 12  # max(10, 11, 12)


def test_rolling_high_missing_column_raises():
    bars = pd.DataFrame({"low": [1, 2, 3]})
    with pytest.raises(ValueError):
        rolling_high(bars, lookback=2, column="high")


def test_rolling_high_custom_column():
    bars = pd.DataFrame({"close": [5, 6, 7, 8]})
    result = rolling_high(bars, lookback=2, column="close", include_today=False)
    assert result.iloc[2] == 6  # max(closes[0:2]) = max(5, 6)
    assert result.iloc[3] == 7  # max(closes[1:3]) = max(6, 7)
