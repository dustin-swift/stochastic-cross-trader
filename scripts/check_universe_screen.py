#!/usr/bin/env python3
"""Daily universe screen (spec §2, §6a) — Finviz-backed.

Screening (market cap, price, avg volume, % off 52wk high, price vs SMA50)
happens entirely outside this repo: the user builds the screen visually in
the Finviz Elite web UI and exports it to CSV by hand (see README). This
script's job is: check that export isn't stale or missing, load it, cap
candidates per sector, and write data/candidates.json.

No earnings/catalyst filtering happens here (2026-08-14, at the user's
direction — briefly tried sourcing it from the export's own "Earnings Date"
column, reverted the same day): that column turned out not to reliably mean
"next upcoming report" — a past-looking date just means the last report
already happened and the next one isn't scheduled yet, which is genuinely
ambiguous to screen on at the full-candidate-list level (346 of 544 rows in
a real export were past-dated this way). The safer, simpler design: don't
try to filter earnings for the whole daily list at all, live or CSV-sourced.
Earnings avoidance instead happens ONLY on the tiny handful of candidates
that actually confirm a stochastic entry signal in a given hourly cycle —
see `scripts/filter_entry_earnings.py` and the hourly-signal-check /
daily-stochastic-check skills, which do a real-time `get_earnings_results`
check right before buying. `--earnings-input` still exists below as a raw
capability (some caller could still pass earnings data to filter on), but
neither scheduled skill calls it anymore.

Also resets data/pending_entries.json to empty on every successful run (spec
§3, 2026-08-04 dual-cross entry revision, see lib.signals.advance_pending_entry)
-- a %K/%D crossing setup mid-confirmation from yesterday is stale intraday
state and shouldn't carry into today's candidate list.

Usage:
  python3 scripts/check_universe_screen.py
  python3 scripts/check_universe_screen.py --config config/strategy.yaml --data-dir data
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


def _parse_atr(row: dict) -> float | None:
    """Prefers "Average True Range" (the original Finviz Elite column name);
    falls back to "ATR" (seen in a leaner export, 2026-08-14) when the former
    is absent or blank. Either way, a missing/unparseable value leaves
    "atr14" as None rather than crashing -- the live entry lifecycle already
    treats a None atr14 as "can't size a stop, skip this entry" (see
    check_hourly_signals.py), not a hard error.
    """
    raw = row.get("Average True Range")
    if raw is None or raw.strip() == "":
        raw = row.get("ATR")
    if raw is None or raw.strip() == "":
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _normalize(rows: list[dict]) -> list[dict]:
    """Finviz CSV rows use "Ticker"/"Sector" (and whatever else the user's
    export includes) — add lowercase "symbol"/"sector" keys for cap_per_sector
    and downstream consumers, keeping the original columns alongside.

    "sector" defaults to "UNKNOWN" when the column is absent (2026-08-14:
    Sector is no longer a required column at all -- see providers/finviz.py).

    Also parses ATR (see `_parse_atr`) into a clean float "atr14" field --
    None when the source column/cell can't supply a value, which downstream
    code already treats as "not known," not a crash.
    """
    normalized = []
    for row in rows:
        normalized.append({
            **row,
            "symbol": row["Ticker"],
            "sector": row.get("Sector", "UNKNOWN"),
            "atr14": _parse_atr(row),
        })
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
        "(or bare date strings for backward compat). Omit to skip earnings filtering entirely -- not used "
        "by either scheduled skill (2026-08-14): earnings avoidance happens only on the tiny confirmed-"
        "signal shortlist, live, right before buying (see scripts/filter_entry_earnings.py).",
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
    # Pending dual-cross entry state (spec §3, 2026-08-04 revision) is
    # intraday-only -- a %K/%D crossing setup from yesterday's candidate list
    # and price context shouldn't silently carry into today's. The daily
    # screen is the natural place to reset it, since it already runs once per
    # morning before the first hourly check of the day.
    store.save_pending_entries({})

    json.dump(candidates, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
