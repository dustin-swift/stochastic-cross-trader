#!/usr/bin/env python3
"""MA Pullback / Breakout-Retest agent's daily universe screen (spec §2) --
Finviz-backed, mirrors `check_universe_screen.py`'s structure but simpler: no
sector cap, no earnings filtering at the screen level. The screen itself
(price > 20/50/200-day MA, new 52-week high, avg volume > 500k, price > $5)
is built by hand in the Finviz Elite UI, same convention as the stochastic
system -- see README.

This script's job is: check the MA agent's own Finviz export
(`data/ma_pullback/finviz_export.csv`, separate file from the stochastic
system's) isn't stale or missing, load it, normalize rows to
`{symbol, sector, atr14 (optional passthrough), ...}`, and print them --
`scripts/check_ma_daily_scan.py` is what actually folds these into the
persisted watchlist (this script doesn't write watchlist.json itself, it
just supplies the day's fresh candidate list as input to that script, same
separation the stochastic system has between check_universe_screen.py ->
candidates.json and check_hourly_signals.py's later use of it).

No earnings filtering here at all (matches spec's daily-scan note "never
places trades" -- this script doesn't even reach that far, it's purely a
screen loader).

Usage:
  python3 scripts/check_ma_universe_screen.py
  python3 scripts/check_ma_universe_screen.py --config config/ma_pullback_strategy.yaml --data-dir data/ma_pullback
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.alerts import send_alert
from lib.config import load_ma_config
from lib.logging_utils import EventLogger
from providers.finviz import check_freshness, load_universe


def _normalize(rows: list[dict]) -> list[dict]:
    """Finviz CSV rows use "Ticker"/"Sector" -- add lowercase "symbol"/"sector"
    keys, keeping the original columns alongside. "sector" defaults to
    "UNKNOWN" when the column is absent, same convention as the stochastic
    system's check_universe_screen.py._normalize.
    """
    normalized = []
    for row in rows:
        normalized.append({
            **row,
            "symbol": row["Ticker"],
            "sector": row.get("Sector", "UNKNOWN"),
        })
    return normalized


def run(csv_path: str, today: date, logger: EventLogger, config: dict | None = None) -> list[dict]:
    try:
        check_freshness(csv_path, today=today)
    except (FileNotFoundError, ValueError) as exc:
        logger.log("ma_universe_screen_blocked", csv_path=csv_path, reason=str(exc))
        send_alert("ma_universe_screen_blocked", str(exc), config=config)
        raise

    raw_rows = load_universe(csv_path)
    candidates = _normalize(raw_rows)

    if not candidates:
        message = f"MA pullback Finviz export at {csv_path} loaded successfully but produced 0 candidates."
        logger.log("ma_universe_screen_empty", csv_path=csv_path, input_count=len(raw_rows))
        send_alert("ma_universe_screen_empty", message, config=config)

    logger.log(
        "ma_universe_screen",
        csv_path=csv_path,
        input_count=len(raw_rows),
        output_count=len(candidates),
        symbols=[c["symbol"] for c in candidates],
    )
    return candidates


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/ma_pullback_strategy.yaml")
    parser.add_argument("--data-dir", default="data/ma_pullback")
    args = parser.parse_args()

    config = load_ma_config(args.config)
    screening = config["screening"]
    logger = EventLogger(args.data_dir)

    try:
        candidates = run(
            csv_path=screening["finviz_csv_path"],
            today=date.today(),
            logger=logger,
            config=config,
        )
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    json.dump(candidates, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
