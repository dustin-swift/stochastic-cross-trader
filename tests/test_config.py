import pytest
import yaml

from lib.config import load_config, load_ma_config

VALID_CFG = {
    "live": False,
    "account_number": "849995824",
    "screening": {
        "finviz_csv_path": "data/finviz_export.csv",
        "max_candidates_per_sector": 5,
    },
    "stochastic": {
        "k_period": 14,
        "k_smooth": 3,
        "d_period": 3,
        "oversold_threshold": 20,
        "overbought_threshold": 80,
    },
    "atr": {"period": 14, "stop_multiplier": 1.5},
    "sizing": {"per_trade_usd": 100, "max_price_per_share": 150, "max_positions": 10},
    "risk": {"max_daily_loss_pct": 3},
    "order_lifecycle": {"poll_timeout_seconds": 30, "poll_interval_seconds": 5},
    "alerts": {"provider": "slack"},
}


def _write(tmp_path, cfg_dict, name="strategy.yaml"):
    path = tmp_path / name
    with path.open("w") as f:
        yaml.safe_dump(cfg_dict, f)
    return path


def test_loads_the_real_repo_config():
    cfg = load_config("config/strategy.yaml")
    assert cfg["live"] is False
    assert cfg["account_number"] == "849995824"
    assert cfg["stochastic"]["k_period"] == 14


def test_load_valid_config_roundtrip(tmp_path):
    path = _write(tmp_path, VALID_CFG)
    cfg = load_config(path)
    assert cfg == VALID_CFG


def test_loads_the_real_daily_config():
    # config/strategy_daily.yaml (the daily-stochastic-check paper track,
    # 2026-08-13) must always load cleanly and must always be a permanent
    # dry-run track -- unlike config/strategy.yaml, `live: True` here would
    # be a real bug, not just an unflipped default.
    cfg = load_config("config/strategy_daily.yaml")
    assert cfg["live"] is False
    assert cfg["stochastic"]["k_period"] == 14
    assert cfg["market_filter"]["fast_sma_period"] == 20
    assert cfg["market_filter"]["slow_sma_period"] == 200


def test_missing_file_raises():
    with pytest.raises(FileNotFoundError):
        load_config("does/not/exist.yaml")


def test_missing_top_level_key_raises(tmp_path):
    bad = dict(VALID_CFG)
    del bad["risk"]
    path = _write(tmp_path, bad)
    with pytest.raises(ValueError, match="risk"):
        load_config(path)


def test_wrong_type_raises(tmp_path):
    bad = {**VALID_CFG, "live": "false"}  # string, not bool
    path = _write(tmp_path, bad)
    with pytest.raises(ValueError, match="live"):
        load_config(path)


def test_missing_nested_key_raises(tmp_path):
    bad = {**VALID_CFG, "sizing": {"per_trade_usd": 100, "max_price_per_share": 150}}  # missing max_positions
    path = _write(tmp_path, bad)
    with pytest.raises(ValueError, match="max_positions"):
        load_config(path)


def test_non_positive_max_positions_raises(tmp_path):
    bad = {**VALID_CFG, "sizing": {"per_trade_usd": 100, "max_price_per_share": 150, "max_positions": 0}}
    path = _write(tmp_path, bad)
    with pytest.raises(ValueError, match="max_positions"):
        load_config(path)


def test_non_positive_max_price_per_share_raises(tmp_path):
    bad = {**VALID_CFG, "sizing": {"per_trade_usd": 100, "max_price_per_share": 0, "max_positions": 10}}
    path = _write(tmp_path, bad)
    with pytest.raises(ValueError, match="max_price_per_share"):
        load_config(path)


# --- load_ma_config (MA Pullback / Breakout-Retest agent) ---

VALID_MA_CFG = {
    "live": False,
    "account_number": "849995824",
    "screening": {"finviz_csv_path": "data/ma_pullback/finviz_export.csv"},
    "breakout": {
        "lookback_days": 252,
        "max_breakout_age_days": 35,
        "failed_breakout_depth": 0.10,
        "min_separation_days": 1,
        "min_extension_pct": 0.01,
    },
    "entry": {
        "late_entry_extension_cap": 0.05,
        "gap_cap_atr_multiple": 1.75,
        "volume_confirm_lookback": 20,
        "atr_regime_lookback": 20,
        "atr_regime_multiple": 1.5,
    },
    "trend": {"sma_fast": 50, "sma_slow": 200, "slope_lookback_bars": 5},
    "atr": {"period": 14},
    "stop_target": {
        "stop_atr_multiple": 2.5,
        "stop_retest_buffer_atr": 0.25,
        "partial_profit_r_multiple": 1.5,
        "partial_profit_pct": 0.45,
        "chandelier_atr_multiple": 2.5,
    },
    "exit_timing": {"trend_invalidation_grace_days": 3, "time_stop_days": 10},
    "sizing": {"per_trade_usd": 100, "max_price_per_share": 150, "max_positions": 10},
    "risk": {"max_daily_loss_pct": 3},
    "order_lifecycle": {"poll_timeout_seconds": 30, "poll_interval_seconds": 5},
    "alerts": {"provider": "slack"},
}


def test_loads_the_real_ma_config():
    cfg = load_ma_config("config/ma_pullback_strategy.yaml")
    assert cfg["live"] is False
    assert cfg["breakout"]["lookback_days"] == 252
    assert cfg["sizing"]["per_trade_usd"] == 100
    assert cfg["sizing"]["max_price_per_share"] == 150
    assert cfg["sizing"]["max_positions"] == 10


def test_load_valid_ma_config_roundtrip(tmp_path):
    path = _write(tmp_path, VALID_MA_CFG, name="ma_pullback_strategy.yaml")
    cfg = load_ma_config(path)
    assert cfg == VALID_MA_CFG


def test_ma_config_missing_file_raises():
    with pytest.raises(FileNotFoundError):
        load_ma_config("does/not/exist.yaml")


def test_ma_config_missing_top_level_key_raises(tmp_path):
    bad = dict(VALID_MA_CFG)
    del bad["breakout"]
    path = _write(tmp_path, bad, name="ma_pullback_strategy.yaml")
    with pytest.raises(ValueError, match="breakout"):
        load_ma_config(path)


def test_ma_config_missing_nested_key_raises(tmp_path):
    bad = {**VALID_MA_CFG, "breakout": {"lookback_days": 252}}  # missing the rest
    path = _write(tmp_path, bad, name="ma_pullback_strategy.yaml")
    with pytest.raises(ValueError, match="max_breakout_age_days"):
        load_ma_config(path)


def test_ma_config_wrong_type_raises(tmp_path):
    bad = {**VALID_MA_CFG, "live": "false"}  # string, not bool
    path = _write(tmp_path, bad, name="ma_pullback_strategy.yaml")
    with pytest.raises(ValueError, match="live"):
        load_ma_config(path)


def test_ma_config_non_positive_per_trade_usd_raises(tmp_path):
    bad = {**VALID_MA_CFG, "sizing": {"per_trade_usd": 0, "max_price_per_share": 150, "max_positions": 10}}
    path = _write(tmp_path, bad, name="ma_pullback_strategy.yaml")
    with pytest.raises(ValueError, match="per_trade_usd"):
        load_ma_config(path)


def test_ma_config_non_positive_max_price_per_share_raises(tmp_path):
    bad = {**VALID_MA_CFG, "sizing": {"per_trade_usd": 100, "max_price_per_share": 0, "max_positions": 10}}
    path = _write(tmp_path, bad, name="ma_pullback_strategy.yaml")
    with pytest.raises(ValueError, match="max_price_per_share"):
        load_ma_config(path)


def test_ma_config_non_positive_max_positions_raises(tmp_path):
    bad = {**VALID_MA_CFG, "sizing": {"per_trade_usd": 100, "max_price_per_share": 150, "max_positions": 0}}
    path = _write(tmp_path, bad, name="ma_pullback_strategy.yaml")
    with pytest.raises(ValueError, match="max_positions"):
        load_ma_config(path)


def test_ma_config_partial_profit_pct_out_of_range_raises(tmp_path):
    bad = {**VALID_MA_CFG, "stop_target": {**VALID_MA_CFG["stop_target"], "partial_profit_pct": 1.5}}
    path = _write(tmp_path, bad, name="ma_pullback_strategy.yaml")
    with pytest.raises(ValueError, match="partial_profit_pct"):
        load_ma_config(path)
