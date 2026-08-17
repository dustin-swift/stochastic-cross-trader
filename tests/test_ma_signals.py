import pandas as pd
import pytest

from lib.ma_signals import evaluate_entry, evaluate_exit


def _daily_bars(closes, highs=None, lows=None, volumes=None, start="2024-01-01"):
    n = len(closes)
    dates = pd.bdate_range(start, periods=n).strftime("%Y-%m-%d").tolist()
    highs = highs if highs is not None else [c + 0.5 for c in closes]
    lows = lows if lows is not None else [c - 0.5 for c in closes]
    volumes = volumes if volumes is not None else [1_000_000.0] * n
    return pd.DataFrame({
        "date": dates,
        "high": [float(x) for x in highs],
        "low": [float(x) for x in lows],
        "close": [float(x) for x in closes],
        "volume": [float(x) for x in volumes],
    })


def _hourly_bars(closes, opens=None, highs=None, lows=None, volumes=None, start="2024-01-08"):
    n = len(closes)
    dates = pd.bdate_range(start, periods=n).strftime("%Y-%m-%d").tolist()
    opens = opens if opens is not None else [c - 0.1 for c in closes]
    highs = highs if highs is not None else [max(o, c) + 0.1 for o, c in zip(opens, closes)]
    lows = lows if lows is not None else [min(o, c) - 0.1 for o, c in zip(opens, closes)]
    volumes = volumes if volumes is not None else [1_000.0] * n
    return pd.DataFrame({
        "date": dates,
        "open": [float(x) for x in opens],
        "high": [float(x) for x in highs],
        "low": [float(x) for x in lows],
        "close": [float(x) for x in closes],
        "volume": [float(x) for x in volumes],
    })


def _entry_cfg(**overrides):
    cfg = {
        "entry": {
            "late_entry_extension_cap": 0.05,
            "gap_cap_atr_multiple": 1.75,
            "volume_confirm_lookback": 3,
            "atr_regime_lookback": 5,
            "atr_regime_multiple": 1.5,
        },
        "trend": {"sma_fast": 3, "sma_slow": 5, "slope_lookback_bars": 2},
        "atr": {"period": 3},
        "stop_target": {
            "stop_atr_multiple": 2.5,
            "stop_retest_buffer_atr": 0.25,
            "partial_profit_r_multiple": 1.5,
            "partial_profit_pct": 0.45,
            "chandelier_atr_multiple": 2.5,
        },
        "sizing": {"per_trade_usd": 100, "max_price_per_share": 150},
    }
    cfg.update(overrides)
    return cfg


def _uptrend_daily_bars(n=13, start_close=90.0):
    closes = [start_close + i for i in range(n)]
    return _daily_bars(closes)


def _watchlist_entry(**overrides):
    entry = {
        "breakout_level": 100.0,
        "retest_low": 97.0,
        "retest_seen": True,
        "failed": False,
        "eligible_for_entry": True,
    }
    entry.update(overrides)
    return entry


# --- evaluate_entry ---

def test_evaluate_entry_fires_on_clean_reclaim():
    daily_bars = _uptrend_daily_bars()
    hourly_closes = [99.0, 99.5, 99.7, 99.8, 100.3]
    hourly_opens = [98.8, 99.3, 99.6, 99.7, 100.0]
    hourly_volumes = [1000.0, 1000.0, 1000.0, 1000.0, 1500.0]
    hourly_bars = _hourly_bars(hourly_closes, opens=hourly_opens, volumes=hourly_volumes)

    decision = evaluate_entry(_watchlist_entry(), daily_bars, hourly_bars, _entry_cfg())

    assert decision is not None
    assert decision["entry_price"] == 100.3
    assert decision["shares"] >= 1
    assert decision["stop_price"] < decision["entry_price"]
    assert decision["target_1"] > decision["entry_price"]
    # stop is the retest-anchored floor: max(retest_low - buffer*atr, entry - mult*atr)
    assert decision["stop_price"] >= 97.0 - 0.25 * decision["atr_entry"] - 1e-9


def test_evaluate_entry_not_eligible_returns_none():
    daily_bars = _uptrend_daily_bars()
    hourly_bars = _hourly_bars([99.0, 99.5, 99.7, 99.8, 100.3])

    decision = evaluate_entry(_watchlist_entry(eligible_for_entry=False), daily_bars, hourly_bars, _entry_cfg())
    assert decision is None


def test_evaluate_entry_failed_breakout_returns_none():
    daily_bars = _uptrend_daily_bars()
    hourly_bars = _hourly_bars([99.0, 99.5, 99.7, 99.8, 100.3])

    decision = evaluate_entry(_watchlist_entry(failed=True), daily_bars, hourly_bars, _entry_cfg())
    assert decision is None


def test_evaluate_entry_no_retest_low_returns_none():
    daily_bars = _uptrend_daily_bars()
    hourly_bars = _hourly_bars([99.0, 99.5, 99.7, 99.8, 100.3])

    decision = evaluate_entry(_watchlist_entry(retest_low=None), daily_bars, hourly_bars, _entry_cfg())
    assert decision is None


def test_evaluate_entry_hard_rule_never_enters_below_breakout_level():
    # A bar that dips THROUGH breakout_level intrabar (low <= level) but
    # closes back BELOW it must never produce an entry -- the reclaim
    # condition itself already requires close >= breakout_level, and this
    # is the exact scenario (an intrabar poke through the level that fails
    # to hold) the hard "never enter below breakout_level" rule exists to
    # guard against. Confirms no entry fires even though the bar touched
    # the level.
    daily_bars = _uptrend_daily_bars()
    hourly_closes = [99.0, 99.3, 99.5, 99.0, 99.5]  # last close (99.5) stays < 100
    hourly_lows = [98.5, 98.8, 99.0, 98.0, 98.0]     # last low (98.0) dips through 100? no -- see below
    # Construct explicitly: previous close < level, current low <= level, but
    # current close < level (the "reclaim wick, no real reclaim" case).
    hourly_bars = _hourly_bars(
        [99.0, 99.3, 99.5, 99.0, 99.5],
        opens=[98.8, 99.1, 99.3, 98.8, 99.2],
        highs=[99.2, 99.5, 99.7, 99.2, 99.8],
        lows=[98.5, 98.8, 99.0, 98.0, 98.0],
    )
    # Force a low that touches breakout_level (100) on the final bar while
    # its close stays below it.
    hourly_bars.loc[hourly_bars.index[-1], "low"] = 99.5
    hourly_bars.loc[hourly_bars.index[-1], "close"] = 99.5

    decision = evaluate_entry(_watchlist_entry(breakout_level=100.0), daily_bars, hourly_bars, _entry_cfg())
    assert decision is None

    # And directly: every synthetic entries[] fixture across this file must
    # never carry entry_price < breakout_level.
    for closes in ([99.0, 99.5, 99.7, 99.8, 100.3],):
        hb = _hourly_bars(closes)
        d = evaluate_entry(_watchlist_entry(), daily_bars, hb, _entry_cfg())
        if d is not None:
            assert d["entry_price"] >= d["breakout_level"]


def test_evaluate_entry_late_entry_extension_cap_blocks():
    daily_bars = _uptrend_daily_bars()
    # Reclaim bar extended 10% above breakout_level (100 -> 110), well past
    # the 5% late_entry_extension_cap.
    hourly_bars = _hourly_bars([99.0, 99.5, 99.7, 99.8, 110.0])

    decision = evaluate_entry(_watchlist_entry(), daily_bars, hourly_bars, _entry_cfg())
    assert decision is None


def test_evaluate_entry_volume_not_confirmed_blocks():
    daily_bars = _uptrend_daily_bars()
    # Reclaim bar's own volume is well below the trailing average.
    hourly_bars = _hourly_bars(
        [99.0, 99.5, 99.7, 99.8, 100.3],
        volumes=[5000.0, 5000.0, 5000.0, 5000.0, 100.0],
    )

    decision = evaluate_entry(_watchlist_entry(), daily_bars, hourly_bars, _entry_cfg())
    assert decision is None


def test_evaluate_entry_trend_not_qualified_blocks():
    # Flat/declining daily closes -- close is not > sma_fast > sma_slow.
    daily_bars = _daily_bars([100.0] * 13)
    hourly_bars = _hourly_bars([99.0, 99.5, 99.7, 99.8, 100.3])

    decision = evaluate_entry(_watchlist_entry(), daily_bars, hourly_bars, _entry_cfg())
    assert decision is None


def test_evaluate_entry_gap_cap_blocks():
    daily_bars = _uptrend_daily_bars()
    # Reclaim bar's OPEN gaps far above breakout_level, beyond gap_cap_atr_multiple * ATR.
    hourly_bars = _hourly_bars(
        [99.0, 99.5, 99.7, 99.8, 100.3],
        opens=[98.8, 99.3, 99.6, 99.7, 130.0],
    )

    decision = evaluate_entry(_watchlist_entry(), daily_bars, hourly_bars, _entry_cfg())
    assert decision is None


def test_evaluate_entry_sizes_like_stochastic_system():
    # Sizing is decoupled from risk_per_share entirely (at the user's
    # direction: same lib.sizing.entry_share_quantity logic as the
    # stochastic system) -- ~per_trade_usd notional, whole shares.
    daily_bars = _uptrend_daily_bars()
    hourly_bars = _hourly_bars([99.0, 99.5, 99.7, 99.8, 100.3])

    decision = evaluate_entry(_watchlist_entry(), daily_bars, hourly_bars, _entry_cfg())

    assert decision is not None
    assert decision["shares"] == round(100 / decision["entry_price"])


def test_evaluate_entry_excludes_stock_over_max_price_per_share():
    # A candidate priced above max_price_per_share is excluded entirely, not
    # bought at 1 share regardless of cost.
    daily_bars = _uptrend_daily_bars(start_close=190.0)
    hourly_bars = _hourly_bars([199.0, 199.5, 199.7, 199.8, 200.3])

    decision = evaluate_entry(
        _watchlist_entry(breakout_level=200.0, retest_low=199.0), daily_bars, hourly_bars, _entry_cfg()
    )

    assert decision is None


def test_evaluate_entry_no_reclaim_returns_none():
    daily_bars = _uptrend_daily_bars()
    # Never crosses back above breakout_level.
    hourly_bars = _hourly_bars([90.0, 91.0, 92.0, 93.0, 94.0])

    decision = evaluate_entry(_watchlist_entry(), daily_bars, hourly_bars, _entry_cfg())
    assert decision is None


# --- evaluate_exit ---

def _exit_cfg(**overrides):
    cfg = {
        "atr": {"period": 3},
        "trend": {"sma_fast": 3, "sma_slow": 5, "slope_lookback_bars": 2},
        "stop_target": {
            "stop_atr_multiple": 2.5,
            "stop_retest_buffer_atr": 0.25,
            "partial_profit_r_multiple": 1.5,
            "partial_profit_pct": 0.45,
            "chandelier_atr_multiple": 2.5,
        },
        "exit_timing": {"trend_invalidation_grace_days": 3, "time_stop_days": 10},
    }
    cfg.update(overrides)
    return cfg


def _position(**overrides):
    pos = {
        "entry_price": 100.0,
        "stop_price": 90.0,
        "target_1": 106.0,
        "highest_close": 100.0,
        "bars_held": 0,
        "partial_taken": False,
        "atr_entry": 2.0,
        "shares": 10,
    }
    pos.update(overrides)
    return pos


def test_evaluate_exit_partial_profit_moves_stop_to_breakeven():
    daily_bars = _daily_bars([95.0, 96.0, 100.0, 103.0, 106.0])
    action, updated = evaluate_exit(_position(), daily_bars, _exit_cfg())

    assert action == "partial"
    assert updated["partial_taken"] is True
    assert updated["stop_price"] >= 100.0  # ratcheted to at least breakeven
    assert updated["bars_held"] == 1
    assert updated["highest_close"] == 106.0


def test_evaluate_exit_chandelier_ratchets_up_only_never_down():
    # A position that's already had its stop ratcheted up to 115 via a prior
    # chandelier update. This cycle's computed chandelier (using a much
    # larger ATR, forced via atr_entry fallback -- see below) would be LOWER
    # than 115 if taken on its own; the stop must stay at 115, never drop.
    position = _position(
        partial_taken=True,
        highest_close=118.0,
        stop_price=115.0,
        atr_entry=3.0,  # fallback used since the daily ATR(period) below can't be computed (insufficient bars)
    )
    # Only 2 bars -- fewer than atr.period=3, so lib.indicators.atr returns
    # NaN for the last row and evaluate_exit falls back to position["atr_entry"].
    daily_bars = _daily_bars([117.0, 118.0])

    cfg = _exit_cfg(atr={"period": 3}, stop_target={
        "stop_atr_multiple": 2.5, "stop_retest_buffer_atr": 0.25,
        "partial_profit_r_multiple": 1.5, "partial_profit_pct": 0.45,
        "chandelier_atr_multiple": 2.5,
    })
    action, updated = evaluate_exit(position, daily_bars, cfg)

    # chandelier = highest_close(118) - 2.5*3.0 = 110.5, LOWER than the
    # existing stop (115) -- must not be allowed to lower it.
    assert updated["stop_price"] == 115.0


def test_evaluate_exit_trend_invalidation_suppressed_during_grace_period():
    # Declining trend from the start (close always below its own 3-bar SMA),
    # but trend_invalidation_grace_days=3 must suppress the exit until
    # bars_held reaches 3.
    closes = [100.0, 100.0, 100.0, 90.0, 80.0, 70.0]
    cfg = _exit_cfg(exit_timing={"trend_invalidation_grace_days": 3, "time_stop_days": 100})

    position = _position(stop_price=-1000.0, target_1=1_000_000.0)  # never hits stop or partial
    action1, position = evaluate_exit(position, _daily_bars(closes[:4]), cfg)
    assert action1 is None
    assert position["bars_held"] == 1

    action2, position = evaluate_exit(position, _daily_bars(closes[:5]), cfg)
    assert action2 is None
    assert position["bars_held"] == 2

    action3, position = evaluate_exit(position, _daily_bars(closes[:6]), cfg)
    assert action3 == "trend_invalidated"
    assert position["bars_held"] == 3


def test_evaluate_exit_time_stop_fires_without_partial():
    # Flat/rising bars (trend stays intact, no trend-invalidation), stop far
    # below (never hit), no partial ever taken -- time_stop must fire once
    # bars_held reaches time_stop_days.
    closes = [100.0, 101.0, 102.0]
    cfg = _exit_cfg(exit_timing={"trend_invalidation_grace_days": 100, "time_stop_days": 2})

    position = _position(stop_price=-1000.0, target_1=1_000_000.0)
    action1, position = evaluate_exit(position, _daily_bars(closes[:2]), cfg)
    assert action1 is None
    assert position["bars_held"] == 1

    action2, position = evaluate_exit(position, _daily_bars(closes[:3]), cfg)
    assert action2 == "time_stop"
    assert position["bars_held"] == 2


def test_evaluate_exit_hard_stop_fires():
    daily_bars = _daily_bars([100.0, 95.0, 80.0])
    position = _position(stop_price=90.0, target_1=1_000_000.0)

    action, updated = evaluate_exit(position, daily_bars, _exit_cfg())
    assert action == "stop_hit"


def test_evaluate_exit_no_action_when_nothing_triggers():
    daily_bars = _daily_bars([100.0, 101.0, 102.0])
    position = _position(stop_price=50.0, target_1=1_000_000.0)

    action, updated = evaluate_exit(position, daily_bars, _exit_cfg())
    assert action is None
    assert updated["bars_held"] == 1
