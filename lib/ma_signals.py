"""Entry/exit decision logic for the MA Pullback / Breakout-Retest agent
(spec §4-6). Pure functions over already-fetched pandas bars -- no network
calls, mirrors the reference implementation's `hourly_monitor.py` but built
on `lib.indicators`/`lib.sizing` instead of hand-rolled list math.

Market-regime and earnings/catalyst vetoes are deliberately NOT evaluated
here -- those are cross-cutting concerns the calling script applies via the
already-generic, reused modules (`scripts/check_market_trend.py` against
`config/strategy.yaml`'s SPY signal, `scripts/filter_entry_earnings.py`),
same separation-of-concerns the stochastic system already uses between pure
signal logic (`lib/signals.py`) and shared gates checked at the script/skill
level. See the plan / README for the full reasoning.
"""
from __future__ import annotations

import pandas as pd

from lib.indicators import atr, sma
from lib.sizing import entry_share_quantity


def evaluate_entry(
    watchlist_entry: dict | None,
    daily_bars: pd.DataFrame,
    hourly_bars: pd.DataFrame,
    config: dict,
) -> dict | None:
    """Evaluate a single watchlist symbol's hourly reclaim-entry trigger.

    `daily_bars`: daily OHLCV through yesterday's close (or today's still-
    forming session, doesn't matter -- only used for trend/ATR context, which
    is computed off the last COMPLETE row the caller supplies), oldest-first.
    `hourly_bars`: hourly OHLCV through the current (just-closed) hourly bar,
    oldest-first. Both need columns open/high/low/close (daily_bars also
    needs volume for the ATR-regime avg... actually ATR itself only needs
    high/low/close; hourly_bars needs volume for the volume-confirmation
    check).

    Returns a dict with the entry decision + computed levels, or `None` if no
    entry (any veto fails, or watchlist_entry isn't eligible at all).
    """
    if not watchlist_entry or not watchlist_entry.get("eligible_for_entry"):
        return None
    if watchlist_entry.get("failed"):
        return None

    breakout_level = watchlist_entry["breakout_level"]
    retest_low = watchlist_entry.get("retest_low")
    if retest_low is None:
        # Shouldn't happen if eligible_for_entry is True (retest_seen implies
        # retest_low was set), but guard rather than assume.
        return None

    hourly_bars = hourly_bars.reset_index(drop=True)
    hc = hourly_bars["close"].astype(float)
    hl = hourly_bars["low"].astype(float)
    ho = hourly_bars["open"].astype(float)
    hi = len(hc) - 1
    if hi < 1:
        return None

    # --- reclaim trigger (hourly) ---
    reclaim = bool(hc.iloc[hi] >= breakout_level and (hc.iloc[hi - 1] < breakout_level or hl.iloc[hi] <= breakout_level))
    if not reclaim:
        return None

    # Hard, never-relaxed rule: never enter below breakout_level -- called
    # out explicitly in the spec even though `reclaim` already implies this
    # (hc.iloc[hi] >= breakout_level is the first half of the reclaim check
    # above), because this is the single most important behavior to get
    # right and a future refactor of the reclaim condition must not
    # accidentally drop it.
    if hc.iloc[hi] < breakout_level:
        return None

    entry_cfg = config["entry"]
    extension = (float(hc.iloc[hi]) - breakout_level) / breakout_level
    if extension > entry_cfg["late_entry_extension_cap"]:
        return None

    # --- trend qualification (daily, Stage-2-style: close > sma50 > sma200,
    # with sma200 confirmed rising over slope_lookback_bars) ---
    trend_cfg = config["trend"]
    daily_bars = daily_bars.reset_index(drop=True)
    dc = daily_bars["close"].astype(float)
    di = len(dc) - 1

    s50_series = sma(daily_bars, period=trend_cfg["sma_fast"], column="close")
    s200_series = sma(daily_bars, period=trend_cfg["sma_slow"], column="close")
    s50 = s50_series.iloc[di]
    s200 = s200_series.iloc[di]
    slope_lookback = trend_cfg["slope_lookback_bars"]
    s200_prior = s200_series.iloc[di - slope_lookback] if di - slope_lookback >= 0 else float("nan")

    if pd.isna(s50) or pd.isna(s200) or pd.isna(s200_prior):
        return None
    if not (float(dc.iloc[di]) > s50 > s200 and s200 > s200_prior):
        return None

    # --- volume confirmation (reclaim-bar volume vs. trailing hourly avg) ---
    if "volume" not in hourly_bars.columns:
        return None
    hv = hourly_bars["volume"].astype(float)
    vol_avg_series = hv.rolling(window=entry_cfg["volume_confirm_lookback"], min_periods=1).mean()
    vol_avg = vol_avg_series.iloc[hi]
    if pd.isna(vol_avg) or hv.iloc[hi] < vol_avg:
        return None

    # --- ATR regime check (daily): today's ATR must not have spiked far
    # above its own recent average ---
    atr_period = config["atr"]["period"]
    atr_series = atr(daily_bars, period=atr_period)
    a14 = atr_series.iloc[di]
    if pd.isna(a14):
        return None
    a14 = float(a14)

    atr_regime_lookback = entry_cfg["atr_regime_lookback"]
    atr_hist = atr_series.iloc[max(0, di - atr_regime_lookback + 1): di + 1].dropna()
    atr_avg = float(atr_hist.mean()) if len(atr_hist) else a14
    if a14 > entry_cfg["atr_regime_multiple"] * atr_avg:
        return None

    # --- gap cap: skip if today's open gapped too far past the trigger ---
    gap_size = float(ho.iloc[hi]) - breakout_level
    if gap_size > entry_cfg["gap_cap_atr_multiple"] * a14:
        return None

    # --- compute entry/stop/target ---
    stop_cfg = config["stop_target"]
    entry_price = float(hc.iloc[hi])
    stop_price = max(
        retest_low - stop_cfg["stop_retest_buffer_atr"] * a14,
        entry_price - stop_cfg["stop_atr_multiple"] * a14,
    )
    risk_per_share = entry_price - stop_price
    if risk_per_share <= 0:
        return None

    # Whole-share sizing (at the user's direction: same logic as the
    # stochastic system, not the ATR-risk-based sizing the original spec
    # draft used) -- target ~per_trade_usd notional, capped/excluded above
    # max_price_per_share. Decoupled from risk_per_share entirely; the stop
    # distance above only drives stop_price/target_1, not share count.
    sizing_cfg = config["sizing"]
    shares = entry_share_quantity(entry_price, sizing_cfg["per_trade_usd"], sizing_cfg["max_price_per_share"])
    if shares is None:
        return None

    entry_date = str(hourly_bars["date"].iloc[hi]) if "date" in hourly_bars.columns else None

    return {
        "entry_price": entry_price,
        "entry_date": entry_date,
        "breakout_level": breakout_level,
        "stop_price": stop_price,
        "risk_per_share": risk_per_share,
        "target_1": entry_price + stop_cfg["partial_profit_r_multiple"] * risk_per_share,
        "highest_close": entry_price,
        "bars_held": 0,
        "partial_taken": False,
        "atr_entry": a14,
        "shares": shares,
    }


def evaluate_exit(position: dict, daily_bars: pd.DataFrame, config: dict) -> tuple[str | None, dict]:
    """Evaluate a single open position's exit conditions off the latest
    completed daily bar. Mirrors the reference `hourly_monitor.evaluate_exit`
    closely -- this part of the reference was already correct/idiomatic, per
    the plan, so it's ported faithfully rather than redesigned.

    Returns `(action, updated_position)`:
    - `action` is `None` (no exit, but the position dict below still needs
      persisting -- bars_held/highest_close/stop_price may have changed),
      `"partial"` (sell `stop_target.partial_profit_pct` of shares, stop
      moves to breakeven, position stays open), or a full-exit reason:
      `"trend_invalidated"`, `"time_stop"`, `"stop_hit"`.
    - `updated_position`: a NEW dict (the input `position` is not mutated in
      place) with `bars_held`/`highest_close`/`stop_price`/`partial_taken`
      advanced by one bar.
    """
    position = dict(position)
    daily_bars = daily_bars.reset_index(drop=True)
    dc = daily_bars["close"].astype(float)
    di = len(dc) - 1

    position["bars_held"] = position.get("bars_held", 0) + 1
    position["highest_close"] = max(position.get("highest_close", position["entry_price"]), float(dc.iloc[di]))

    atr_period = config["atr"]["period"]
    atr_series = atr(daily_bars, period=atr_period)
    a14_raw = atr_series.iloc[di]
    a14 = float(a14_raw) if not pd.isna(a14_raw) else position.get("atr_entry")

    stop_cfg = config["stop_target"]
    exit_cfg = config["exit_timing"]
    trend_cfg = config["trend"]

    # --- partial profit at 1.5R (or configured multiple) ---
    partial_action = None
    if not position.get("partial_taken") and float(dc.iloc[di]) >= position["target_1"]:
        position["partial_taken"] = True
        position["stop_price"] = max(position["stop_price"], position["entry_price"])
        partial_action = "partial"

    # --- trailing chandelier stop, post-partial only -- ratchets UP only,
    # never loosens (max() with the existing stop_price enforces this) ---
    if position.get("partial_taken") and a14 is not None:
        chandelier = position["highest_close"] - stop_cfg["chandelier_atr_multiple"] * a14
        position["stop_price"] = max(position["stop_price"], chandelier)

    # --- trend invalidation, gated by the grace period (spec-corrected: a
    # close < sma50 exit can't fire until bars_held >= grace_days, to avoid
    # whipsaw on noise right after a fresh reclaim) ---
    s50_series = sma(daily_bars, period=trend_cfg["sma_fast"], column="close")
    s50 = s50_series.iloc[di]
    if (
        not pd.isna(s50)
        and float(dc.iloc[di]) < s50
        and position["bars_held"] >= exit_cfg["trend_invalidation_grace_days"]
    ):
        return "trend_invalidated", position

    # --- time stop: only if no partial has been taken yet ---
    if not position.get("partial_taken") and position["bars_held"] >= exit_cfg["time_stop_days"]:
        return "time_stop", position

    # --- hard stop ---
    if float(dc.iloc[di]) < position["stop_price"]:
        return "stop_hit", position

    return partial_action, position
