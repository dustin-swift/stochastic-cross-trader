#!/usr/bin/env python3
"""Daily universe screen (spec §2, §6a) — Finviz-backed.

Screening (market cap, price, avg volume, % off 52wk high, price vs SMA50)
happens entirely outside this repo: the user builds the screen visually in
the Finviz Elite web UI and exports it to CSV by hand (see README). This
script's job is: check that export isn't stale or missing, load it, cap
candidates per sector, optionally exclude candidates with earnings too close
(catalyst avoidance), and write data/candidates.json.

Earnings data (--earnings-input) is optional and fetched by the caller via
Robinhood MCP (get_earnings_results, one call per symbol) — this script does
no network calls. It's applied AFTER the sector cap, not before: checking
earnings for the full raw Finviz universe (which can be 50-100+ rows) would
reintroduce the same per-symbol-call scaling problem that Finviz replaced
create_scan for in the first place. The tradeoff is that an excluded slot
isn't backfilled from the same sector — acceptable for now, revisit if this
meaningfully shrinks the daily list in practice.

Earnings entries carry timing ("am"=BMO, "pm"=AMC, from get_earnings_results'
report.timing) so lib.catalysts can compute the correct trigger date — see
lib/catalysts.py's module docstring for the BMO/AMC-aware rule. A candidate
is excluded only on the exact day that would otherwise force-exit it almost
immediately (a single-day buffer, not a multi-day window) — entries on
earlier days are intentionally allowed through so they still capture the
run-up into the report.

Usage:
  python3 scripts/check_universe_screen.py
  python3 scripts/check_universe_screen.py --config config/strategy.yaml --data-dir data
  echo '{"AAPL": [{"date": "2026-08-15", "timing": "am"}], "MSFT": []}' | python3 scripts/check_universe_screen.py --earnings-input -
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.alerts import send_alert
from lib.catalysts import filter_earnings_entry_blocked
from lib.config import load_config
from lib.logging_utils import EventLogger
from lib.state import StateStore
from lib.universe import cap_per_sector
from providers.finviz import check_freshness, load_universe


def _normalize(rows: list[dict]) -> list[dict]:
    """Finviz CSV rows use "Ticker"/"Sector" (and whatever else the user's
    export includes) — add lowercase "symbol"/"sector" keys for cap_per_sector
    and downstream consumers, keeping the original columns alongside.
    """
    normalized = []
    for row in rows:
        normalized.append({**row, "symbol": row["Ticker"], "sector": row.get("Sector", "UNKNOWN")})
    return normalized


def _load_earnings_input(arg: str | None) -> dict[str, list[dict | str]] | None:
    if arg is None:
        return None
    if arg == "-":
        return json.load(sys.stdin)
    with open(arg) as f:
        return json.load(f)


def run(
    csv_path: str,
    max_per_sector: int,
    today: date,
    logger: EventLogger,
    earnings_by_symbol: dict[str, list[dict | str]] | None = None,
    catalysts_enabled: bool = True,
) -> list[dict]:
    try:
        check_freshness(csv_path, today=today)
    except (FileNotFoundError, ValueError) as exc:
        logger.log("universe_screen_blocked", csv_path=csv_path, reason=str(exc))
        send_alert("universe_screen_blocked", str(exc))
        raise

    raw_rows = load_universe(csv_path)
    candidates = cap_per_sector(_normalize(raw_rows), max_per_sector=max_per_sector)

    if catalysts_enabled and earnings_by_symbol is not None:
        candidates, excluded = filter_earnings_entry_blocked(candidates, earnings_by_symbol, as_of=today)
        if excluded:
            logger.log(
                "universe_screen_earnings_excluded",
                symbols=[c["symbol"] for c in excluded],
                reasons={c["symbol"]: c["earnings_exclusion_reason"] for c in excluded},
            )

    if not candidates:
        message = f"Finviz export at {csv_path} loaded successfully but produced 0 candidates."
        logger.log("universe_screen_empty", csv_path=csv_path, input_count=len(raw_rows))
        send_alert("universe_screen_empty", message)

    logger.log(
        "universe_screen",
        csv_path=csv_path,
        input_count=len(raw_rows),
        output_count=len(candidates),
        symbols=[c["symbol"] for c in candidates],
    )
    return candidates


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/strategy.yaml")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument(
        "--earnings-input",
        default=None,
        help="JSON file (or '-' for stdin) mapping symbol -> list of {\"date\", \"timing\"} report objects "
        "(or bare date strings for backward compat). Omit to skip earnings filtering entirely.",
    )
    args = parser.parse_args()

    config = load_config(args.config)
    screening = config["screening"]
    catalysts = config.get("catalysts", {})
    logger = EventLogger(args.data_dir)

    earnings_by_symbol = _load_earnings_input(args.earnings_input)

    try:
        candidates = run(
            csv_path=screening["finviz_csv_path"],
            max_per_sector=screening["max_candidates_per_sector"],
            today=date.today(),
            logger=logger,
            earnings_by_symbol=earnings_by_symbol,
            catalysts_enabled=catalysts.get("enabled", True),
        )
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    store = StateStore(args.data_dir)
    store.save_candidates(candidates)

    json.dump(candidates, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
