import pytest

from lib.risk import circuit_breaker_tripped


def test_not_tripped_when_profitable():
    assert circuit_breaker_tripped(1500, realized_pnl_today=20, unrealized_pnl_today=10, max_daily_loss_pct=3) is False


def test_not_tripped_small_loss_within_threshold():
    # threshold at 3% of 1500 = -45; -30 total loss is within bounds
    assert circuit_breaker_tripped(1500, realized_pnl_today=-10, unrealized_pnl_today=-20, max_daily_loss_pct=3) is False


def test_tripped_at_exact_threshold():
    # exactly -45 == -3% of 1500 -> tripped (<=, not strictly <)
    assert circuit_breaker_tripped(1500, realized_pnl_today=-20, unrealized_pnl_today=-25, max_daily_loss_pct=3) is True


def test_tripped_beyond_threshold():
    assert circuit_breaker_tripped(1500, realized_pnl_today=-40, unrealized_pnl_today=-30, max_daily_loss_pct=3) is True


def test_zero_account_value_trips_breaker_not_an_error():
    # unfunded account / funding not yet settled -> nothing to size against,
    # block new entries rather than crash the whole hourly cycle.
    assert circuit_breaker_tripped(0, 0, 0, max_daily_loss_pct=3) is True


def test_negative_account_value_raises():
    with pytest.raises(ValueError):
        circuit_breaker_tripped(-100, 0, 0, max_daily_loss_pct=3)


def test_invalid_max_daily_loss_pct_raises():
    with pytest.raises(ValueError):
        circuit_breaker_tripped(1500, 0, 0, max_daily_loss_pct=0)
