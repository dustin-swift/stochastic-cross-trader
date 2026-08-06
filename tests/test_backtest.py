import pytest

from lib.backtest import Trade, _summarize, run_backtest

CFG = {
    "stochastic": {"k_period": 3, "k_smooth": 1, "d_period": 2, "oversold_threshold": 20, "overbought_threshold": 80},
    "atr": {"stop_multiplier": 1.5},
    "sizing": {"per_trade_usd": 100, "max_positions": 10},
}

# Real ISO8601 hourly timestamps, exactly what Robinhood's get_equity_historicals
# returns (`begins_at`) -- exercises the engine's actual sort/lookup path
# (lib.backtest sorts by parsed timestamp, not raw string) rather than a
# synthetic label scheme that could mask a sort-order bug.
_T = [f"2026-07-{20 + i // 24:02d}T{i % 24:02d}:00:00Z" for i in range(11)]


def _bar(t, close, high=1000, low=100):
    return {"begins_at": t, "high": high, "low": low, "close": close}


# high=1000/low=100 fixed for every bar -> raw %K = 100*(close-100)/900,
# independent of rolling-window content (same trick used throughout the
# lib.signals/lib.indicators test suites, just rescaled so a small ATR-based
# stop can sit safely below the fixed low without ever spuriously triggering
# a stop-loss: close = 100 + 9*raw_k, so raw_k = (close-100)/9).
#
# Shared entry setup (indices 0-4): closes [150, 150, 244, 226, 370] give
# raw %K = [NaN, NaN, 16, 14, 30] (k_smooth=1 so %K == raw %K) and, with
# d_period=2, %D = [NaN, NaN, NaN, 15, 22]. Bar 4: dual-cross entry (spec §3,
# 2026-08-04 revision, see lib.signals.advance_pending_entry) -- %K(30)
# crosses above 20 from %K(14) on the prior bar, AND %D(22) *also* crosses
# above 20 on this same bar, with %K(30) under the 55 invalidation cap -> fires
# in a single one-shot call with no persisted `pending` state needed.
_ENTRY_CLOSES = [150, 150, 244, 226, 370]
_ENTRY_TIME = _T[4]
_ENTRY_PRICE = 370.0


def _entry_bars():
    return [_bar(t, c) for t, c in zip(_T, _ENTRY_CLOSES)]


def test_entry_then_stop_loss():
    bars = _entry_bars() + [_bar(_T[5], 460)]  # close value irrelevant; low=100 triggers the stop regardless
    # Small ATR -> stop sits close to entry, above the fixed low(100) -> triggers immediately next bar.
    data = {"XXX": {"bars": bars, "atr_series": [{"begins_at": _T[4], "value": 1.0}]}}

    result = run_backtest(data, CFG)

    assert result["open_positions"] == []
    assert len(result["trades"]) == 1
    trade = result["trades"][0]
    assert trade["symbol"] == "XXX"
    assert trade["entry_time"] == _ENTRY_TIME
    assert trade["entry_price"] == pytest.approx(_ENTRY_PRICE)
    assert trade["exit_time"] == _T[5]
    assert trade["exit_reason"] == "stop_loss"
    assert trade["exit_price"] == pytest.approx(368.5)  # 370 - 1.5*1.0
    assert trade["qty"] == pytest.approx(100 / 370)
    assert trade["pnl"] == pytest.approx((368.5 - 370) * (100 / 370))


def test_entry_then_signal_exit_bearish_crossover():
    # Continue past entry: closes [460, 190] -> raw %K = [40, 10], %D = [35, 25].
    # t6: prev %K(40)>=prev %D(35), cur %K(10)<cur %D(25) -> bearish crossover.
    bars = _entry_bars() + [_bar(_T[5], 460), _bar(_T[6], 190)]
    # Large ATR -> stop (370 - 1.5*200 = 70) sits below the fixed low(100), so
    # the stop-loss check never spuriously fires; only the signal exit can.
    data = {"XXX": {"bars": bars, "atr_series": [{"begins_at": _T[4], "value": 200.0}]}}

    result = run_backtest(data, CFG)

    assert len(result["trades"]) == 1
    trade = result["trades"][0]
    assert trade["exit_time"] == _T[6]
    assert trade["exit_reason"] == "signal_exit"
    assert trade["exit_price"] == pytest.approx(190.0)
    assert trade["stop_price"] == pytest.approx(70.0)
    assert result["open_positions"] == []


def test_trend_filter_suppresses_signal_exit_while_price_above_sma():
    # Continue past entry with a shallow pullback: closes [400, 390] ->
    # k5=33.33, k6=32.22, d5=31.67, d6=32.78 -> prev_k(33.33)>=prev_d(31.67)
    # and cur_k(32.22)<cur_d(32.78) -> a genuine bearish crossover under
    # STATE_NORMAL. But close(390) at t6 is still above the 3-bar SMA of
    # (370, 400, 390) = 386.67 -> trend intact, so the crossover must be
    # suppressed and the position stays open.
    bars = _entry_bars() + [_bar(_T[5], 400), _bar(_T[6], 390)]
    cfg = {**CFG, "trend_filter": {"enabled": True, "sma_period": 3}}
    data = {"XXX": {"bars": bars, "atr_series": [{"begins_at": _T[4], "value": 200.0}]}}

    result = run_backtest(data, cfg)

    assert result["trades"] == []
    assert len(result["open_positions"]) == 1
    assert result["open_positions"][0]["exit_reason"] == "open_at_end"


def test_trend_filter_disabled_reproduces_old_crossover_behavior():
    # Identical bars/crossover to the suppression test above, but with the
    # filter explicitly disabled -- the exit must fire exactly as it did
    # before spec §4a existed.
    bars = _entry_bars() + [_bar(_T[5], 400), _bar(_T[6], 390)]
    cfg = {**CFG, "trend_filter": {"enabled": False}}
    data = {"XXX": {"bars": bars, "atr_series": [{"begins_at": _T[4], "value": 200.0}]}}

    result = run_backtest(data, cfg)

    assert len(result["trades"]) == 1
    trade = result["trades"][0]
    assert trade["exit_time"] == _T[6]
    assert trade["exit_reason"] == "signal_exit"
    assert trade["exit_price"] == pytest.approx(390.0)


def test_overbought_hold_suppresses_whipsaw_then_exits_on_double_drop():
    # Ramp into overbought, whipsaw while both lines stay >= 80, then both
    # drop below 80 together. raw %K sequence from t4: 30, 60, 88, 91, 97, 82,
    # 70 (each bar's raw %K depends only on its own close, per the fixed
    # high/low trick, so this is unaffected by the entry bar's %K moving from
    # 24 to 30 under the dual-cross revision); %D: 22, 45, 74, 89.5, 94, 89.5, 76.
    extra_closes = [640, 892, 919, 973, 838, 730]  # -> raw %K 60,88,91,97,82,70
    bars = _entry_bars() + [_bar(t, c) for t, c in zip(_T[5:], extra_closes)]
    # Large ATR -> stop (370 - 1.5*200 = 70) sits below the fixed low(100), so
    # the stop-loss check never spuriously fires before the overbought-hold
    # exit gets its chance.
    data = {"XXX": {"bars": bars, "atr_series": [{"begins_at": _T[4], "value": 200.0}]}}

    result = run_backtest(data, CFG)

    assert len(result["trades"]) == 1
    trade = result["trades"][0]
    # Never exits during t5-t9 despite a would-be bearish crossover at t9
    # (prev %K=97>=prev %D=94, cur %K=82<cur %D=89.5) -- suppressed because
    # %K=82 is still >= the overbought threshold, not a "both below 80" exit.
    assert trade["exit_time"] == _T[10]
    assert trade["exit_reason"] == "overbought_hold_exit"
    assert trade["exit_price"] == pytest.approx(730.0)
    assert trade["pnl"] == pytest.approx((730.0 - 370.0) * (100 / 370))


def test_missing_atr_at_entry_bar_skips_the_entry():
    bars = _entry_bars()
    data = {"XXX": {"bars": bars, "atr_series": []}}  # no ATR available anywhere

    result = run_backtest(data, CFG)

    assert result["trades"] == []
    assert result["open_positions"] == []


def test_trade_excursion_tracking():
    trade = Trade(symbol="X", entry_time="t0", entry_price=100.0, qty=1.0, stop_price=90.0)
    assert trade.mfe_pct is None
    assert trade.mae_pct is None

    trade.update_excursion(bar_high=105.0, bar_low=98.0)
    assert trade.mfe_pct == pytest.approx(5.0)
    assert trade.mae_pct == pytest.approx(-2.0)

    # A quieter bar (lower high, higher low than the extremes so far) must not
    # erase the prior extremes -- MFE/MAE are running high/low-water marks.
    trade.update_excursion(bar_high=103.0, bar_low=99.0)
    assert trade.mfe_pct == pytest.approx(5.0)
    assert trade.mae_pct == pytest.approx(-2.0)

    # A new low, still no new high.
    trade.update_excursion(bar_high=101.0, bar_low=95.0)
    assert trade.mfe_pct == pytest.approx(5.0)
    assert trade.mae_pct == pytest.approx(-5.0)

    # A new high, still no new low.
    trade.update_excursion(bar_high=110.0, bar_low=97.0)
    assert trade.mfe_pct == pytest.approx(10.0)
    assert trade.mae_pct == pytest.approx(-5.0)


def test_backtest_records_mfe_mae_on_closed_trade():
    # Reuses the stop-loss fixture (fixed high=1000/low=100 for every bar --
    # see the trick explained above). Entry at close 370; the very next bar
    # both triggers the stop AND is the only post-entry bar tracked, so MFE
    # comes from that bar's high (1000) and MAE from its low (100) -- entry
    # bar itself must be excluded from the excursion (no bar before it exists
    # to have set an MFE/MAE off of, and the fill happens at that bar's close,
    # so there's no known intrabar excursion prior to the fill).
    bars = _entry_bars() + [_bar(_T[5], 460)]
    data = {"XXX": {"bars": bars, "atr_series": [{"begins_at": _T[4], "value": 1.0}]}}

    result = run_backtest(data, CFG)

    trade = result["trades"][0]
    assert trade["mfe_pct"] == pytest.approx((1000 - 370) / 370 * 100)
    assert trade["mae_pct"] == pytest.approx((100 - 370) / 370 * 100)


def test_position_still_open_at_end_of_window():
    bars = _entry_bars()
    data = {"XXX": {"bars": bars, "atr_series": [{"begins_at": _T[4], "value": 150.0}]}}

    result = run_backtest(data, CFG)

    assert result["trades"] == []
    assert len(result["open_positions"]) == 1
    open_pos = result["open_positions"][0]
    assert open_pos["symbol"] == "XXX"
    assert open_pos["exit_reason"] == "open_at_end"
    assert open_pos["exit_price"] is None
    # No bar exists after entry in this fixture (entry is on the last bar) --
    # excursion tracking never ran, so MFE/MAE stay unset rather than some
    # placeholder like 0.
    assert open_pos["mfe_pct"] is None
    assert open_pos["mae_pct"] is None


def test_slot_cap_and_alphabetical_tie_break():
    bars = _entry_bars()
    cfg = {**CFG, "sizing": {"per_trade_usd": 100, "max_positions": 1}}
    data = {
        "BBB": {"bars": bars, "atr_series": [{"begins_at": _T[4], "value": 150.0}]},
        "AAA": {"bars": bars, "atr_series": [{"begins_at": _T[4], "value": 150.0}]},
    }

    result = run_backtest(data, cfg)

    assert result["trades"] == []
    assert len(result["open_positions"]) == 1
    assert result["open_positions"][0]["symbol"] == "AAA"  # alphabetically first got the one slot


def test_empty_input_returns_empty_result():
    result = run_backtest({}, CFG)
    assert result == {
        "trades": [],
        "open_positions": [],
        "summary": {
            "trade_count": 0,
            "win_count": 0,
            "loss_count": 0,
            "win_rate_pct": None,
            "total_pnl": 0.0,
            "avg_pnl": None,
            "avg_win": None,
            "avg_loss": None,
            "max_drawdown": 0.0,
            "by_exit_reason": {},
        },
    }


def test_summarize_stats():
    trades = [
        Trade(symbol="A", entry_time="t0", entry_price=100, qty=1, stop_price=90, exit_time="t1", exit_price=110, exit_reason="signal_exit"),
        Trade(symbol="B", entry_time="t0", entry_price=100, qty=1, stop_price=90, exit_time="t1", exit_price=95, exit_reason="stop_loss"),
        Trade(symbol="C", entry_time="t0", entry_price=50, qty=2, stop_price=40, exit_time="t1", exit_price=45, exit_reason="stop_loss"),
    ]

    summary = _summarize(trades)

    assert summary["trade_count"] == 3
    assert summary["win_count"] == 1
    assert summary["loss_count"] == 2
    assert summary["win_rate_pct"] == pytest.approx(100 / 3)
    assert summary["total_pnl"] == pytest.approx(-5.0)
    assert summary["avg_pnl"] == pytest.approx(-5.0 / 3)
    assert summary["avg_win"] == pytest.approx(10.0)
    assert summary["avg_loss"] == pytest.approx(-7.5)
    assert summary["max_drawdown"] == pytest.approx(-15.0)
    assert summary["by_exit_reason"] == {"signal_exit": 1, "stop_loss": 2}


def test_entry_lookback_bars_agrees_when_basing_run_is_uniformly_oversold():
    # Basing-pattern requirement (2026-08-06): entry_lookback_bars now
    # requires ALL bars in the window to be oversold (.all(), not .any()),
    # not just "the crossing bar's own predecessor" -- see lib/signals.py's
    # module docstring for the full history of this change. When the entire
    # pre-crossing run genuinely is flat-and-oversold, widening the window
    # doesn't change the outcome, since every bar in it already qualifies --
    # this isolates that agreement case specifically, with enough padding
    # that the NaN-window effect (see the short-history test below) doesn't
    # confound it. See test_entry_lookback_bars_requires_full_basing_run
    # below for the case where widening DOES change the outcome.
    padded_closes = [150, 150, 150, 150, 150, 244, 226, 370]  # 3 extra flat warmup bars
    padded_times = _T[:8]
    bars = [_bar(t, c) for t, c in zip(padded_times, padded_closes)]
    data = {"XXX": {"bars": bars, "atr_series": [{"begins_at": padded_times[-1], "value": 150.0}]}}

    result_default = run_backtest(data, CFG)
    result_wide = run_backtest(data, {**CFG, "stochastic": {**CFG["stochastic"], "entry_lookback_bars": 5}})

    assert len(result_default["open_positions"]) == len(result_wide["open_positions"]) == 1
    assert result_default["open_positions"][0]["entry_price"] == result_wide["open_positions"][0]["entry_price"]


def test_entry_lookback_bars_requires_full_basing_run():
    # k sequence [.., 25, 14, 30]: the bar two back from the crossing bar
    # (k=25) is NOT oversold, only the immediately-preceding one (k=14) is --
    # a single-bar dip, not a real 2-bar basing pattern. lookback_bars=1
    # (only checks the immediate predecessor) still fires; lookback_bars=2
    # (requires both prior bars oversold) correctly blocks it.
    closes = [150, 150, 325, 226, 370]
    bars = [_bar(t, c) for t, c in zip(_T[:5], closes)]
    data = {"XXX": {"bars": bars, "atr_series": [{"begins_at": _T[4], "value": 150.0}]}}

    result_default = run_backtest(data, CFG)  # entry_lookback_bars=1 (implicit)
    result_2bar = run_backtest(data, {**CFG, "stochastic": {**CFG["stochastic"], "entry_lookback_bars": 2}})

    assert len(result_default["open_positions"]) == 1
    assert result_2bar["open_positions"] == []
    assert result_2bar["trades"] == []


def test_entry_lookback_bars_can_suppress_entry_when_history_is_short():
    # The flip side of the test above: with only 5 total bars (2 NaN warmup +
    # 3 valid k's), a lookback window of 5+1=6 bars reaches back into the NaN
    # warmup region and fails the validity check entirely -- suppressing an
    # entry that lookback_bars=1 (window size 2) takes fine. This is a real,
    # reproducible effect of the config value; it's just not the "deeper
    # oversold dip" filter it sounds like.
    bars = _entry_bars()  # only 5 bars total
    data = {"XXX": {"bars": bars, "atr_series": [{"begins_at": _T[4], "value": 150.0}]}}

    result_default = run_backtest(data, CFG)
    result_wide = run_backtest(data, {**CFG, "stochastic": {**CFG["stochastic"], "entry_lookback_bars": 5}})

    assert len(result_default["open_positions"]) == 1
    assert len(result_wide["open_positions"]) == 0


# -- trailing-stop research variant (config["trailing_stop"]) ------------
#
# Uses k_period=1 (each bar's raw %K depends only on that bar's own
# high/low/close, no rolling window) specifically so the price path used for
# MFE/trailing calculations can be realistic and independently designed from
# the earlier tests' "fixed high/low" stochastic-control trick -- that trick
# pins every bar's high/low to the same constants, which is perfect for
# controlling %K but unusable here since MFE/trailing needs genuinely
# evolving highs/lows. overbought_threshold=999 (unreachable) keeps the
# position in STATE_NORMAL throughout so only entry/trailing-stop/ordinary
# signal-exit logic is in play, not the overbought-hold state machine.
_TRAIL_T = [f"2026-07-20T{h:02d}:00:00Z" for h in range(7)]


def _trail_bar(t, high, low, close):
    return {"begins_at": t, "high": high, "low": low, "close": close}


def _trail_bars():
    # bar0: k=24 (warmup); bar1: k=16 (oversold prior); bar2: k=52, d=34
    # (entry -- dual-cross, spec §3 2026-08-04 revision: %K(52) crosses above
    # 20 from %K(16), AND %D(34) also crosses above 20 on the same bar, with
    # %K(52) under the 55 invalidation cap) -- entry_price=26.
    # bar3-5: tight-range rally bars (high-low=2, less than the trail
    # distance of 4, so no bar can self-trigger the stop it just set); each
    # bar's own %K is nudged to a distinct value (90, 92.5, 95) rather than
    # repeating the same one, since an exact repeat produces a float-level
    # %K/%D tie in the crossover check that can spuriously fire a same-value
    # "crossover".
    # bar6: a genuine pullback -- low dips below the ratcheted trail level.
    return [
        _trail_bar(_TRAIL_T[0], 50, 0, 12),
        _trail_bar(_TRAIL_T[1], 50, 0, 8),
        _trail_bar(_TRAIL_T[2], 50, 0, 26),
        _trail_bar(_TRAIL_T[3], 30, 28, 29.8),
        _trail_bar(_TRAIL_T[4], 34, 32, 33.85),
        _trail_bar(_TRAIL_T[5], 38, 36, 37.9),
        _trail_bar(_TRAIL_T[6], 35, 33, 33.5),
    ]


def _trail_cfg(**trailing_overrides):
    return {
        "stochastic": {"k_period": 1, "k_smooth": 1, "d_period": 2, "oversold_threshold": 20, "overbought_threshold": 999},
        "atr": {"stop_multiplier": 1.5},
        "sizing": {"per_trade_usd": 100, "max_positions": 10},
        "trailing_stop": {"enabled": True, "activation_pct": 3.0, "atr_multiplier": 2.0, **trailing_overrides},
    }


def _trail_atr_series(value=2.0):
    return [{"begins_at": t, "value": value} for t in _TRAIL_T]


def test_trailing_stop_disabled_by_default_matches_baseline_behavior():
    # No "trailing_stop" key at all in config -- must behave identically to
    # every other test in this file (fixed stop only), not silently default
    # to enabled.
    cfg = {
        "stochastic": {"k_period": 1, "k_smooth": 1, "d_period": 2, "oversold_threshold": 20, "overbought_threshold": 999},
        "atr": {"stop_multiplier": 1.5},
        "sizing": {"per_trade_usd": 100, "max_positions": 10},
    }
    data = {"XXX": {"bars": _trail_bars(), "atr_series": _trail_atr_series()}}

    result = run_backtest(data, cfg)

    trade = result["trades"][0]
    assert trade["exit_reason"] == "signal_exit"
    assert trade["exit_price"] == pytest.approx(33.5)


def test_trailing_stop_ratchets_and_exits_at_trail_level():
    data = {"XXX": {"bars": _trail_bars(), "atr_series": _trail_atr_series()}}

    result = run_backtest(data, _trail_cfg())

    assert len(result["trades"]) == 1
    trade = result["trades"][0]
    assert trade["entry_price"] == pytest.approx(26.0)
    assert trade["stop_price"] == pytest.approx(23.0)  # original fixed stop, untouched
    assert trade["exit_reason"] == "trailing_stop"
    # Running high reaches 38 (bar C); trail = 38 - 2.0*ATR(2.0) = 34.
    assert trade["exit_price"] == pytest.approx(34.0)
    assert trade["pnl"] == pytest.approx((34.0 - 26.0) * (100 / 26.0))


def test_trailing_stop_beats_waiting_for_the_signal_exit_on_this_path():
    # Same exact price path, trailing off vs on: trailing exits at the trail
    # level (34.0) reached intrabar on the pullback bar's low, rather than
    # riding the bar all the way down to its close (33.5) where the ordinary
    # bearish crossover finally catches it. Small difference here, but it's
    # the mechanism the MFE/MAE analysis flagged as promising, demonstrated
    # end-to-end on one concrete path.
    data = {"XXX": {"bars": _trail_bars(), "atr_series": _trail_atr_series()}}
    cfg_on = _trail_cfg()
    cfg_off = {**cfg_on, "trailing_stop": {"enabled": False}}

    trade_on = run_backtest(data, cfg_on)["trades"][0]
    trade_off = run_backtest(data, cfg_off)["trades"][0]

    assert trade_off["exit_reason"] == "signal_exit"
    assert trade_on["exit_reason"] == "trailing_stop"
    assert trade_on["exit_price"] > trade_off["exit_price"]
    assert trade_on["pnl"] > trade_off["pnl"]


def test_trailing_stop_never_loosens_below_the_fixed_stop():
    # A tiny gain that never reaches activation_pct must leave the fixed
    # stop exactly where it started -- no premature arming, no exit.
    small_gain_bars = [
        _trail_bar(_TRAIL_T[0], 50, 0, 12),
        _trail_bar(_TRAIL_T[1], 50, 0, 8),
        _trail_bar(_TRAIL_T[2], 50, 0, 26),
        # close near the bar's own high (not near its low, unlike the entry
        # bar's shape) keeps %K rising rather than dropping, so this doesn't
        # accidentally also trigger an ordinary bearish-crossover signal exit
        # -- the test needs the position to still be open to prove non-arming.
        _trail_bar(_TRAIL_T[3], 26.35, 26.15, 26.33),  # ~1.35% gain, below the 3% activation
    ]
    data = {"XXX": {"bars": small_gain_bars, "atr_series": _trail_atr_series()}}

    result = run_backtest(data, _trail_cfg())

    assert result["trades"] == []
    assert len(result["open_positions"]) == 1
    # Never armed -> still open, not stopped out (26.15 is well above the
    # fixed stop of 23, and the trail never activated to pull the stop up
    # to somewhere that would have mattered here either).
    assert result["open_positions"][0]["exit_reason"] == "open_at_end"


# -- earnings/catalyst avoidance (config["catalysts"]) --------------------


def test_earnings_skips_entry_when_too_close():
    bars = _entry_bars()  # entry would occur at _T[4] (2026-07-20)
    data = {
        "XXX": {
            "bars": bars,
            "atr_series": [{"begins_at": _T[4], "value": 150.0}],
            # Reports 2026-07-21, BMO (bare string = conservative default) ->
            # exit_date is 2026-07-20, exactly the would-be entry bar's date.
            "earnings_report_dates": ["2026-07-21"],
        }
    }

    result = run_backtest(data, CFG)

    assert result["trades"] == []
    assert result["open_positions"] == []  # never entered at all


def test_earnings_does_not_restrict_entry_when_far_enough_out():
    bars = _entry_bars()
    data = {
        "XXX": {
            "bars": bars,
            "atr_series": [{"begins_at": _T[4], "value": 150.0}],
            "earnings_report_dates": ["2026-08-15"],  # far in the future
        }
    }

    result = run_backtest(data, CFG)

    assert len(result["open_positions"]) == 1  # entered normally


def test_earnings_force_exits_open_position_regardless_of_price():
    # Entry bar is 2026-07-20 -- earnings on 2026-07-28 is 8 days out, outside
    # the default 5-day exclusion window, so the entry proceeds normally. A
    # later bar (no bars need exist in between -- the engine just walks the
    # array, bar-count-based, not calendar-based) is dated 2026-07-27, one day
    # before the same earnings date -> within the default 1-day forced-exit
    # window, timestamped 19:00 UTC to land at/after the default
    # forced_exit_utc_hour=19 near_close threshold (2026-08-04 fix -- the
    # force-exit only fires on the LAST regular-session bar of exit_date, not
    # the first; see test below for the "too early in the day" case).
    # close=400 is an arbitrary big jump on that bar -- the forced exit must
    # fire regardless of what price/oscillator would otherwise say.
    later_bar = _bar("2026-07-27T19:00:00Z", 400)
    bars = _entry_bars() + [later_bar]
    data = {
        "XXX": {
            "bars": bars,
            "atr_series": [{"begins_at": _T[4], "value": 150.0}],
            "earnings_report_dates": ["2026-07-28"],
        }
    }

    result = run_backtest(data, CFG)

    assert len(result["trades"]) == 1
    trade = result["trades"][0]
    assert trade["exit_reason"] == "earnings_exit"
    assert trade["exit_time"] == "2026-07-27T19:00:00Z"
    assert trade["exit_price"] == pytest.approx(400.0)


def test_earnings_does_not_force_exit_before_near_close_bar():
    # This is the exact bug confirmed live on BALL (2026-08-03): the forced
    # exit must NOT fire on an early-session bar of exit_date, only the last
    # regular-session bar (hour >= forced_exit_utc_hour, default 19). Same
    # setup as the test above, but the only bar on exit_date is timestamped
    # 14:00 UTC (well before the threshold) -- position must stay open.
    early_bar = _bar("2026-07-27T14:00:00Z", 400)
    bars = _entry_bars() + [early_bar]
    data = {
        "XXX": {
            "bars": bars,
            # Large ATR -> stop (370 - 1.5*200 = 70) sits below the fixed
            # low(100), so the stop-loss check never spuriously fires and
            # masks whether the earnings gate itself is doing the right thing.
            "atr_series": [{"begins_at": _T[4], "value": 200.0}],
            "earnings_report_dates": ["2026-07-28"],
        }
    }

    result = run_backtest(data, CFG)

    assert result["trades"] == []
    assert len(result["open_positions"]) == 1
    assert result["open_positions"][0]["exit_reason"] == "open_at_end"


def test_earnings_absent_key_does_not_restrict_backtest():
    # No "earnings_report_dates" key at all -> must behave exactly like the
    # existing signal-exit baseline test (unrestricted).
    bars = _entry_bars() + [_bar(_T[5], 460), _bar(_T[6], 190)]
    data = {"XXX": {"bars": bars, "atr_series": [{"begins_at": _T[4], "value": 200.0}]}}

    result = run_backtest(data, CFG)

    assert result["trades"][0]["exit_reason"] == "signal_exit"
