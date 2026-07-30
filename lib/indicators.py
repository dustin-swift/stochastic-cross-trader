"""Pure indicator math. No network calls — operates on OHLC data already fetched
via the Robinhood MCP tools (get_equity_historicals).
"""
from __future__ import annotations

import pandas as pd


def stochastic(
    bars: pd.DataFrame,
    k_period: int = 14,
    k_smooth: int = 3,
    d_period: int = 3,
) -> pd.DataFrame:
    """Slow stochastic oscillator (%K, %D) from OHLC bars.

    `bars` must have columns: high, low, close, indexed/ordered oldest-first.

    Raw %K = 100 * (close - lowest_low(k_period)) / (highest_high(k_period) - lowest_low(k_period))
    %K (slow, displayed) = SMA(raw %K, k_smooth)
    %D = SMA(%K, d_period)

    This is the standard "slow stochastic" convention (matches the spec's
    "standard settings 14, 3, 3" — %K period, %K smoothing, %D period).

    Returns a DataFrame with columns [k, d] aligned to `bars`' index. Rows
    before the indicator has enough history are NaN.
    """
    required = {"high", "low", "close"}
    missing = required - set(bars.columns)
    if missing:
        raise ValueError(f"bars is missing required columns: {sorted(missing)}")

    lowest_low = bars["low"].rolling(window=k_period).min()
    highest_high = bars["high"].rolling(window=k_period).max()

    denom = highest_high - lowest_low
    raw_k = 100 * (bars["close"] - lowest_low) / denom
    # Flat range (high == low over the window): stochastic is undefined: leave as
    # NaN rather than divide-by-zero garbage.
    raw_k = raw_k.where(denom != 0)

    k = raw_k.rolling(window=k_smooth).mean()
    d = k.rolling(window=d_period).mean()

    return pd.DataFrame({"k": k, "d": d}, index=bars.index)
