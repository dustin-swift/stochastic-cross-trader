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


def sma(bars: pd.DataFrame, period: int, column: str = "close") -> pd.Series:
    """Simple moving average of `column` over `period` bars (spec §4a trend
    filter). Rows before `period` bars of history exist are NaN.
    """
    if column not in bars.columns:
        raise ValueError(f"bars is missing required column: {column!r}")
    return bars[column].rolling(window=period).mean()


def atr(bars: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range (Wilder's simple-moving-average convention, matching
    the standard daily ATR(14) meaning used elsewhere in this repo — see
    lib.sizing/config["atr"] on the stochastic side). `bars` must have
    columns: high, low, close, indexed/ordered oldest-first.

    True range for bar i = max(high[i]-low[i], |high[i]-close[i-1]|,
    |low[i]-close[i-1]|); the first bar has no prior close, so its true range
    is just high-low. ATR is the simple rolling mean of true range over
    `period` bars — rows before `period` bars of true-range history exist are
    NaN, same NaN-until-warmed-up convention as `sma`/`stochastic`.
    """
    required = {"high", "low", "close"}
    missing = required - set(bars.columns)
    if missing:
        raise ValueError(f"bars is missing required columns: {sorted(missing)}")

    prior_close = bars["close"].shift(1)
    true_range = pd.concat(
        [
            bars["high"] - bars["low"],
            (bars["high"] - prior_close).abs(),
            (bars["low"] - prior_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    # First bar has no prior close -- fall back to that bar's own high-low
    # range rather than leaving it NaN from the shift, matching the reference
    # implementation's treatment (a missing prior close isn't a data gap here,
    # it's just the start of the series).
    true_range.iloc[0] = (bars["high"] - bars["low"]).iloc[0]

    return true_range.rolling(window=period).mean()


def rolling_high(bars: pd.DataFrame, lookback: int, column: str = "high", include_today: bool = False) -> pd.Series:
    """Highest `column` value over the trailing `lookback` bars (the MA
    pullback agent's 52-week/252-day breakout level, see lib.ma_breakout).

    `include_today=False` (the default) excludes the current row from its own
    rolling window -- this is what encodes the spec's core correction: a
    breakout day's own high is never part of the level being broken, only the
    PRIOR `lookback` bars are. `include_today=True` includes the current row
    (an ordinary trailing rolling-max, e.g. for computing the running highest
    high over some other window).

    Rows before `lookback` bars of the relevant history exist are NaN (not a
    guess at a shorter window) -- callers should treat NaN as "not enough
    history to evaluate a breakout yet," same convention as sma/stochastic/atr.
    """
    if column not in bars.columns:
        raise ValueError(f"bars is missing required column: {column!r}")
    if include_today:
        return bars[column].rolling(window=lookback).max()
    return bars[column].shift(1).rolling(window=lookback).max()
