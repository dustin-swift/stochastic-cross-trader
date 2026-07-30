"""Load and validate config/strategy.yaml. All strategy tunables live in that
one file so refinement during testing is a config edit, not a code change
(spec §6a) — this module's job is just to catch a malformed/incomplete config
early with a clear error, not to encode strategy logic.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_REQUIRED_TOP_LEVEL = {
    "live": bool,
    "account_number": str,
    "screening": dict,
    "stochastic": dict,
    "atr": dict,
    "sizing": dict,
    "risk": dict,
    "order_lifecycle": dict,
    "alerts": dict,
}

_REQUIRED_NESTED = {
    "screening": {
        "finviz_csv_path": str,
        "max_candidates_per_sector": int,
    },
    "stochastic": {
        "k_period": int,
        "k_smooth": int,
        "d_period": int,
        "oversold_threshold": (int, float),
        "overbought_threshold": (int, float),
    },
    "atr": {
        "period": int,
        "stop_multiplier": (int, float),
    },
    "sizing": {
        "per_trade_usd": (int, float),
        "max_price_per_share": (int, float),
        "max_positions": int,
    },
    "risk": {
        "max_daily_loss_pct": (int, float),
    },
    "order_lifecycle": {
        "poll_timeout_seconds": (int, float),
        "poll_interval_seconds": (int, float),
    },
    "alerts": {
        "provider": str,
    },
}


def load_config(path: str | Path = "config/strategy.yaml") -> dict[str, Any]:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"strategy config not found: {path}")

    with path.open() as f:
        cfg = yaml.safe_load(f)

    _validate(cfg, path)
    return cfg


def _validate(cfg: Any, path: Path) -> None:
    if not isinstance(cfg, dict):
        raise ValueError(f"{path}: expected a top-level mapping, got {type(cfg).__name__}")

    for key, expected_type in _REQUIRED_TOP_LEVEL.items():
        if key not in cfg:
            raise ValueError(f"{path}: missing required top-level key '{key}'")
        if not isinstance(cfg[key], expected_type):
            raise ValueError(
                f"{path}: '{key}' must be {expected_type.__name__}, got {type(cfg[key]).__name__}"
            )

    for section, fields in _REQUIRED_NESTED.items():
        section_cfg = cfg[section]
        for field, expected_type in fields.items():
            if field not in section_cfg:
                raise ValueError(f"{path}: missing required key '{section}.{field}'")
            if not isinstance(section_cfg[field], expected_type):
                raise ValueError(
                    f"{path}: '{section}.{field}' must be {expected_type}, "
                    f"got {type(section_cfg[field]).__name__}"
                )

    if cfg["sizing"]["max_positions"] <= 0:
        raise ValueError(f"{path}: sizing.max_positions must be positive")
    if cfg["sizing"]["per_trade_usd"] <= 0:
        raise ValueError(f"{path}: sizing.per_trade_usd must be positive")
    if cfg["sizing"]["max_price_per_share"] <= 0:
        raise ValueError(f"{path}: sizing.max_price_per_share must be positive")
