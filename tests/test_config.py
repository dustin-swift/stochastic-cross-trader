import pytest
import yaml

from lib.config import load_config

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
    "sizing": {"per_trade_usd": 100, "max_positions": 10},
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
    bad = {**VALID_CFG, "sizing": {"per_trade_usd": 100}}  # missing max_positions
    path = _write(tmp_path, bad)
    with pytest.raises(ValueError, match="max_positions"):
        load_config(path)


def test_non_positive_max_positions_raises(tmp_path):
    bad = {**VALID_CFG, "sizing": {"per_trade_usd": 100, "max_positions": 0}}
    path = _write(tmp_path, bad)
    with pytest.raises(ValueError, match="max_positions"):
        load_config(path)
