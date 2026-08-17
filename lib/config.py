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

    _validate_schema(cfg, path, _REQUIRED_TOP_LEVEL, _REQUIRED_NESTED)

    if cfg["sizing"]["max_positions"] <= 0:
        raise ValueError(f"{path}: sizing.max_positions must be positive")
    if cfg["sizing"]["per_trade_usd"] <= 0:
        raise ValueError(f"{path}: sizing.per_trade_usd must be positive")
    if cfg["sizing"]["max_price_per_share"] <= 0:
        raise ValueError(f"{path}: sizing.max_price_per_share must be positive")

    return cfg


def _validate_schema(
    cfg: Any,
    path: Path,
    required_top_level: dict[str, type | tuple[type, ...]],
    required_nested: dict[str, dict[str, type | tuple[type, ...]]],
) -> None:
    """Shared top-level/nested-key type-checking loop, factored out so
    `load_config` and `load_ma_config` (see below) validate against their own
    schemas without duplicating this walk. Strategy-specific value checks
    (e.g. "must be positive") stay in each loader, not here -- this is purely
    presence/type checking.
    """
    if not isinstance(cfg, dict):
        raise ValueError(f"{path}: expected a top-level mapping, got {type(cfg).__name__}")

    for key, expected_type in required_top_level.items():
        if key not in cfg:
            raise ValueError(f"{path}: missing required top-level key '{key}'")
        if not isinstance(cfg[key], expected_type):
            raise ValueError(
                f"{path}: '{key}' must be {expected_type.__name__}, got {type(cfg[key]).__name__}"
            )

    for section, fields in required_nested.items():
        section_cfg = cfg[section]
        for field, expected_type in fields.items():
            if field not in section_cfg:
                raise ValueError(f"{path}: missing required key '{section}.{field}'")
            if not isinstance(section_cfg[field], expected_type):
                raise ValueError(
                    f"{path}: '{section}.{field}' must be {expected_type}, "
                    f"got {type(section_cfg[field]).__name__}"
                )


# --- MA Pullback / Breakout-Retest agent config (config/ma_pullback_strategy.yaml) ---
# Its own top-level/nested schema -- breakout/entry/trend/atr/stop_target/
# exit_timing sections instead of the stochastic system's stochastic/atr
# sections -- but reuses `_validate_schema` above rather than duplicating the
# presence/type-checking loop.
_MA_REQUIRED_TOP_LEVEL = {
    "live": bool,
    "account_number": str,
    "screening": dict,
    "breakout": dict,
    "entry": dict,
    "trend": dict,
    "atr": dict,
    "stop_target": dict,
    "exit_timing": dict,
    "sizing": dict,
    "risk": dict,
    "order_lifecycle": dict,
    "alerts": dict,
}

_MA_REQUIRED_NESTED = {
    "screening": {
        "finviz_csv_path": str,
    },
    "breakout": {
        "lookback_days": int,
        "max_breakout_age_days": int,
        "failed_breakout_depth": (int, float),
        "min_separation_days": int,
        "min_extension_pct": (int, float),
    },
    "entry": {
        "late_entry_extension_cap": (int, float),
        "gap_cap_atr_multiple": (int, float),
        "volume_confirm_lookback": int,
        "atr_regime_lookback": int,
        "atr_regime_multiple": (int, float),
    },
    "trend": {
        "sma_fast": int,
        "sma_slow": int,
        "slope_lookback_bars": int,
    },
    "atr": {
        "period": int,
    },
    "stop_target": {
        "stop_atr_multiple": (int, float),
        "stop_retest_buffer_atr": (int, float),
        "partial_profit_r_multiple": (int, float),
        "partial_profit_pct": (int, float),
        "chandelier_atr_multiple": (int, float),
    },
    "exit_timing": {
        "trend_invalidation_grace_days": int,
        "time_stop_days": int,
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


def load_ma_config(path: str | Path = "config/ma_pullback_strategy.yaml") -> dict[str, Any]:
    """Load and validate config/ma_pullback_strategy.yaml -- the MA Pullback /
    Breakout-Retest agent's own tunable config, kept entirely separate from
    `load_config`'s stochastic-system schema (see README's "MA Pullback /
    Breakout-Retest Agent" section). Shares `_validate_schema`'s presence/type
    walk with `load_config` above rather than duplicating it.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"MA pullback strategy config not found: {path}")

    with path.open() as f:
        cfg = yaml.safe_load(f)

    _validate_schema(cfg, path, _MA_REQUIRED_TOP_LEVEL, _MA_REQUIRED_NESTED)

    # Sizing mirrors load_config's stochastic-system checks above -- same
    # whole-share entry_share_quantity sizing logic, reused verbatim (per the
    # user's direction: this agent's sizing should follow the exact same
    # per_trade_usd/max_price_per_share/max_positions logic as the stochastic
    # system, not the ATR-risk-based sizing the original spec draft used).
    if cfg["sizing"]["max_positions"] <= 0:
        raise ValueError(f"{path}: sizing.max_positions must be positive")
    if cfg["sizing"]["per_trade_usd"] <= 0:
        raise ValueError(f"{path}: sizing.per_trade_usd must be positive")
    if cfg["sizing"]["max_price_per_share"] <= 0:
        raise ValueError(f"{path}: sizing.max_price_per_share must be positive")
    if not (0 < cfg["stop_target"]["partial_profit_pct"] < 1):
        raise ValueError(f"{path}: stop_target.partial_profit_pct must be between 0 and 1")

    return cfg
