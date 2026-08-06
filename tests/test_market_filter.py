from lib.market_filter import market_trend_intact


def _bars(closes):
    return [{"high": c, "low": c, "close": c} for c in closes]


def _uptrend_closes(n, start=100.0, step=0.5):
    return [start + i * step for i in range(n)]


def test_market_trend_intact_true_when_fast_above_and_rising():
    # A steady uptrend: fast SMA well above slow SMA, and higher than it was
    # rising_lookback_bars ago.
    closes = _uptrend_closes(210)
    result = market_trend_intact(_bars(closes), fast_period=20, slow_period=200, rising_lookback_bars=5)
    assert result is True


def test_market_trend_intact_false_when_fast_below_slow():
    # A steady downtrend: fast SMA below slow SMA.
    closes = list(reversed(_uptrend_closes(210)))
    result = market_trend_intact(_bars(closes), fast_period=20, slow_period=200, rising_lookback_bars=5)
    assert result is False


def test_market_trend_intact_false_when_above_but_flat():
    # Fast SMA sits above slow SMA (long prior uptrend), but the tail is
    # flat for longer than fast_period -- the entire fast-SMA window is
    # inside the flat region by the end, so it's genuinely constant over
    # the rising_lookback_bars comparison, not just "still drifting up
    # because the window straddles the rising and flat regions." "Above"
    # alone isn't enough.
    closes = _uptrend_closes(200) + [_uptrend_closes(200)[-1]] * 25
    result = market_trend_intact(_bars(closes), fast_period=20, slow_period=200, rising_lookback_bars=5)
    assert result is False


def test_market_trend_intact_none_with_insufficient_history():
    closes = _uptrend_closes(50)  # well short of slow_period=200
    result = market_trend_intact(_bars(closes), fast_period=20, slow_period=200, rising_lookback_bars=5)
    assert result is None


def test_market_trend_intact_none_at_exact_boundary_minus_one():
    # slow_period + rising_lookback_bars - 1 bars: one short of enough.
    closes = _uptrend_closes(200 + 5 - 1)
    result = market_trend_intact(_bars(closes), fast_period=20, slow_period=200, rising_lookback_bars=5)
    assert result is None


def test_market_trend_intact_custom_periods():
    closes = _uptrend_closes(30)
    result = market_trend_intact(_bars(closes), fast_period=5, slow_period=20, rising_lookback_bars=3)
    assert result is True


# -- require_rising=False (2026-08-06 minute-bar revision) ------------------


def test_market_trend_intact_no_rising_check_true_when_above_and_flat():
    # The "above but flat" case that returns False when require_rising=True
    # (see test above) must return True with require_rising=False -- a plain
    # fast-above-slow comparison, no slope requirement at all.
    closes = _uptrend_closes(200) + [_uptrend_closes(200)[-1]] * 25
    result = market_trend_intact(
        _bars(closes), fast_period=20, slow_period=200, require_rising=False
    )
    assert result is True


def test_market_trend_intact_no_rising_check_false_when_below():
    closes = list(reversed(_uptrend_closes(210)))
    result = market_trend_intact(
        _bars(closes), fast_period=20, slow_period=200, require_rising=False
    )
    assert result is False


def test_market_trend_intact_no_rising_check_needs_only_slow_period_bars():
    # With require_rising=False, rising_lookback_bars is irrelevant to the
    # data-availability check -- exactly slow_period bars is enough, unlike
    # the require_rising=True case which needs slow_period + lookback.
    closes = _uptrend_closes(200)
    result = market_trend_intact(
        _bars(closes), fast_period=20, slow_period=200, rising_lookback_bars=5, require_rising=False
    )
    assert result is True


def test_market_trend_intact_no_rising_check_none_when_insufficient():
    closes = _uptrend_closes(150)
    result = market_trend_intact(
        _bars(closes), fast_period=20, slow_period=200, require_rising=False
    )
    assert result is None
