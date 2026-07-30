import math

import numpy as np
import pandas as pd
import pytest

from lib.indicators import stochastic


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
