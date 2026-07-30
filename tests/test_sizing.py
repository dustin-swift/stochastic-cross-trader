import pytest

from lib.sizing import entry_share_quantity


def test_rounds_to_nearest_whole_share():
    # 100 / 60 = 1.67 -> rounds to 2
    assert entry_share_quantity(price=60.0, per_trade_usd=100.0, max_price_per_share=150.0) == 2


def test_rounds_down_when_closer():
    # 100 / 70 = 1.43 -> rounds to 1
    assert entry_share_quantity(price=70.0, per_trade_usd=100.0, max_price_per_share=150.0) == 1


def test_minimum_one_share_never_zero():
    # 100 / 149 = 0.67 -> round() alone would give 1 anyway, but this checks
    # the floor holds even right at the edge of the cap
    assert entry_share_quantity(price=149.0, per_trade_usd=100.0, max_price_per_share=150.0) == 1


def test_price_at_cap_still_allowed():
    assert entry_share_quantity(price=150.0, per_trade_usd=100.0, max_price_per_share=150.0) == 1


def test_price_over_cap_skips_entirely():
    assert entry_share_quantity(price=150.01, per_trade_usd=100.0, max_price_per_share=150.0) is None


def test_price_well_over_cap_skips():
    assert entry_share_quantity(price=650.0, per_trade_usd=100.0, max_price_per_share=150.0) is None


def test_cheap_stock_buys_several_shares():
    # 100 / 20 = 5.0
    assert entry_share_quantity(price=20.0, per_trade_usd=100.0, max_price_per_share=150.0) == 5


def test_price_equal_to_target_buys_one_share():
    assert entry_share_quantity(price=100.0, per_trade_usd=100.0, max_price_per_share=150.0) == 1


def test_zero_price_raises():
    with pytest.raises(ValueError):
        entry_share_quantity(price=0.0, per_trade_usd=100.0, max_price_per_share=150.0)


def test_negative_price_raises():
    with pytest.raises(ValueError):
        entry_share_quantity(price=-10.0, per_trade_usd=100.0, max_price_per_share=150.0)
