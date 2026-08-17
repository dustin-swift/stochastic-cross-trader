#!/usr/bin/env python3
"""Renders dashboard.html from repo state (positions/trade_history/candidates/
logs/config) plus a small live snapshot the caller fetched from the broker.

Everything derivable from data already in the repo (open positions, closed
trades, today's watchlist, strategy config, circuit-breaker status, today's
event-type counts) is computed here. The only things this script *cannot*
get on its own -- because they require a live broker call -- are passed in
via stdin: account totals and current prices for open positions.

Input (JSON, via --input file or stdin):
  {
    "account": {"total_value": 1500.0, "cash": 1500.0, "buying_power": 1500.0,
                "equity_value": 0.0, "realized_pnl_all_time": 0.0},
    "position_prices": {"AAPL": 212.40, ...}   # current price per open-position symbol; omit/empty if no open positions
  }

Output: writes the rendered dashboard to --output (default dashboard/dist.html)
and prints its path to stdout.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.config import load_config, load_ma_config
from lib.state import StateStore

DEFAULT_DAILY_CONFIG = "config/strategy_daily.yaml"
DEFAULT_DAILY_DATA_DIR = "data/daily"
DEFAULT_MA_CONFIG = "config/ma_pullback_strategy.yaml"
DEFAULT_MA_DATA_DIR = "data/ma_pullback"

ROUTINES = [
    {"name": "daily-universe-screen", "schedule": "8:00 AM ET, weekdays"},
    {"name": "hourly-signal-check", "schedule": "Hourly, 9:30 AM – 3:30 PM ET, weekdays"},
    {"name": "daily-stochastic-check", "schedule": "Once, after close, weekdays (paper/dry-run comparison track)"},
    {"name": "daily-ma-scan", "schedule": "8:05 AM ET, weekdays (MA Pullback/Breakout-Retest agent, staggered from daily-universe-screen)"},
    {"name": "hourly-ma-signal-check", "schedule": "Hourly, 9:35 AM – 3:35 PM ET, weekdays (MA Pullback/Breakout-Retest agent, staggered from hourly-signal-check)"},
    {"name": "dashboard-refresh", "schedule": "Hourly, 9:40 AM – 3:40 PM ET, weekdays"},
]


def _load_input(input_arg: str) -> dict:
    if input_arg == "-":
        return json.load(sys.stdin)
    with open(input_arg) as f:
        return json.load(f)


def _todays_event_counts(data_dir: Path, today: str) -> dict[str, int]:
    path = data_dir / "logs" / f"{today}.jsonl"
    counts: dict[str, int] = {}
    if not path.exists():
        return counts
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            event = json.loads(line).get("event", "unknown")
            if isinstance(event, dict):
                event = event.get("event", "unknown")
            counts[event] = counts.get(event, 0) + 1
    return counts


def _latest_circuit_breaker_status(data_dir: Path, lookback_days: int = 3) -> dict:
    logs_dir = data_dir / "logs"
    if not logs_dir.exists():
        return {"tripped": False, "checked_at": None}
    files = sorted(logs_dir.glob("*.jsonl"), reverse=True)[:lookback_days]
    latest = None
    for path in files:
        with path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                if record.get("event") == "circuit_breaker_check":
                    if latest is None or record["timestamp"] > latest["timestamp"]:
                        latest = record
    if latest is None:
        return {"tripped": False, "checked_at": None}
    return {"tripped": bool(latest["tripped"]), "checked_at": latest["timestamp"]}


def _build_daily_section(daily_config_path: str, daily_data_dir: str) -> dict:
    """The daily-stochastic-check track's positions/trade_history for the
    dashboard's second tab (2026-08-13). Never raises: this track is a
    parallel comparison feature added after the dashboard's own launch, so a
    dashboard-refresh run against an older bot-state checkout (or before the
    daily track has ever run once) must still render the hourly tab fine --
    an empty/absent daily config or data dir degrades to an empty section,
    not a build failure.
    """
    try:
        daily_config = load_config(daily_config_path)
    except (FileNotFoundError, ValueError):
        return {"positions": [], "trade_history": [], "config": {"max_positions": 0}, "available": False}

    daily_store = StateStore(daily_data_dir)
    positions = daily_store.load_positions()
    positions_list = [dict(pos, symbol=symbol) for symbol, pos in positions.items()]
    return {
        "positions": positions_list,
        "trade_history": daily_store.load_trade_history(),
        "config": {"max_positions": daily_config["sizing"]["max_positions"]},
        "available": True,
    }


def _build_ma_section(ma_config_path: str, ma_data_dir: str) -> dict:
    """The MA Pullback / Breakout-Retest agent's positions/watchlist/trade
    history for the dashboard's third tab. Same degrades-to-empty pattern as
    `_build_daily_section` above: an absent config or data dir (this agent
    hasn't run yet, or an older bot-state checkout predates it) must not fail
    the whole dashboard build -- it just renders an empty MA tab.
    """
    try:
        ma_config = load_ma_config(ma_config_path)
    except (FileNotFoundError, ValueError):
        return {
            "positions": [],
            "watchlist": [],
            "trade_history": [],
            "config": {"live": False, "per_trade_usd": 0, "max_price_per_share": 0, "max_positions": 0, "max_daily_loss_pct": 0},
            "available": False,
        }

    ma_store = StateStore(ma_data_dir)
    positions = ma_store.load_positions()
    positions_list = [dict(pos, symbol=symbol) for symbol, pos in positions.items()]

    watchlist = ma_store.load_watchlist()
    watchlist_list = [dict(entry, symbol=symbol) for symbol, entry in watchlist.items()]

    return {
        "positions": positions_list,
        "watchlist": watchlist_list,
        "trade_history": ma_store.load_trade_history(),
        "config": {
            "live": ma_config["live"],
            "per_trade_usd": ma_config["sizing"]["per_trade_usd"],
            "max_price_per_share": ma_config["sizing"]["max_price_per_share"],
            "max_positions": ma_config["sizing"]["max_positions"],
            "max_daily_loss_pct": ma_config["risk"]["max_daily_loss_pct"],
        },
        "activity_today": _todays_event_counts(Path(ma_data_dir), datetime.now(timezone.utc).strftime("%Y-%m-%d")),
        "available": True,
    }


def build_data(config: dict, store: StateStore, data_dir: Path, live_snapshot: dict, now: datetime, daily_config_path: str, daily_data_dir: str, ma_config_path: str, ma_data_dir: str) -> dict:
    positions = store.load_positions()
    position_prices = live_snapshot.get("position_prices", {})
    positions_list = []
    for symbol, pos in positions.items():
        entry = dict(pos)
        entry["symbol"] = symbol
        if symbol in position_prices:
            entry["current_price"] = position_prices[symbol]
        positions_list.append(entry)

    candidates_raw = store.load_candidates()
    candidates = [
        {
            "symbol": c.get("symbol"),
            "sector": c.get("sector"),
            "price": c.get("Price"),
            "change": c.get("Change"),
        }
        for c in candidates_raw
    ]

    today = now.strftime("%Y-%m-%d")

    return {
        "generated_at": now.isoformat(),
        "account": live_snapshot.get("account", {}),
        "config": {
            "live": config["live"],
            "per_trade_usd": config["sizing"]["per_trade_usd"],
            "max_positions": config["sizing"]["max_positions"],
            "max_daily_loss_pct": config["risk"]["max_daily_loss_pct"],
            "atr_period": config["atr"]["period"],
            "stop_multiplier": config["atr"]["stop_multiplier"],
            "overbought_threshold": config["stochastic"]["overbought_threshold"],
            "oversold_threshold": config["stochastic"]["oversold_threshold"],
            "catalysts_enabled": config.get("catalysts", {}).get("enabled", True),
        },
        "circuit_breaker": _latest_circuit_breaker_status(data_dir),
        "positions": positions_list,
        "trade_history": store.load_trade_history(),
        "candidates": candidates,
        "routines": ROUTINES,
        "activity_today": _todays_event_counts(data_dir, today),
        "daily": _build_daily_section(daily_config_path, daily_data_dir),
        "ma": _build_ma_section(ma_config_path, ma_data_dir),
    }


def render(template_path: Path, fonts_dir: Path, data: dict) -> str:
    template = template_path.read_text()
    plex_b64 = (fonts_dir / "plex.b64").read_text().strip()
    jbm_b64 = (fonts_dir / "jbmono.b64").read_text().strip()
    return (
        template.replace("__PLEX_B64__", plex_b64)
        .replace("__JBM_B64__", jbm_b64)
        .replace("__DATA_JSON__", json.dumps(data))
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default="-", help="JSON input file, or '-' for stdin (default)")
    parser.add_argument("--config", default="config/strategy.yaml")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--dashboard-dir", default="dashboard", help="Directory containing template.html and fonts/")
    parser.add_argument("--output", default="dashboard/dist.html")
    parser.add_argument("--daily-config", default=DEFAULT_DAILY_CONFIG, help="daily-stochastic-check track's config, for the dashboard's second tab")
    parser.add_argument("--daily-data-dir", default=DEFAULT_DAILY_DATA_DIR, help="daily-stochastic-check track's state dir, for the dashboard's second tab")
    parser.add_argument("--ma-config", default=DEFAULT_MA_CONFIG, help="MA Pullback/Breakout-Retest agent's config, for the dashboard's third tab")
    parser.add_argument("--ma-data-dir", default=DEFAULT_MA_DATA_DIR, help="MA Pullback/Breakout-Retest agent's state dir, for the dashboard's third tab")
    args = parser.parse_args()

    config = load_config(args.config)
    data_dir = Path(args.data_dir)
    store = StateStore(data_dir)
    live_snapshot = _load_input(args.input)
    now = datetime.now(timezone.utc)

    data = build_data(config, store, data_dir, live_snapshot, now, args.daily_config, args.daily_data_dir, args.ma_config, args.ma_data_dir)

    dashboard_dir = Path(args.dashboard_dir)
    html = render(dashboard_dir / "template.html", dashboard_dir / "fonts", data)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html)

    print(str(output_path))


if __name__ == "__main__":
    main()
