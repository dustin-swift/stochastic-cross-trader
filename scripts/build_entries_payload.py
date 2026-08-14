#!/usr/bin/env python3
"""Assembles section 5's candidates payload for check_hourly_signals.py
incrementally, one fetched batch at a time, instead of requiring the calling
agent to hand-transcribe every candidate's bars into one giant JSON object at
the end (2026-08-07, at the user's direction).

Why this exists: with the sector cap removed (2026-08-04), the daily
candidate list can run into the hundreds (456 on 2026-08-07) -- 45+ batched
`get_equity_historicals` calls (Robinhood batches up to 10 symbols/call).
Building the final `{"candidates": [...]}` payload by hand after all of them
have been fetched means the agent re-typing every symbol's full bar series
from its own context into one shell command -- a real risk of a transcribed
digit silently feeding a wrong price into a live buy decision, and in
practice not even reliably completable in one pass (confirmed live: a cycle
gave up after 12/46 batches rather than risk it, logged as
`candidate_sweep_truncated`). Processing one batch (<=10 symbols) at a time
keeps each individual step small and mechanical, and this script -- not the
agent -- does the actual merging/reshaping, so nothing past "paste this one
batch's response verbatim" ever depends on manual accuracy.

Two modes, used in sequence for one hourly cycle's section 5:

**--append** (run once per fetched batch): reads one raw
`get_equity_historicals` response (the exact tool output, unmodified) via
--input/stdin, reshapes each symbol's bars (`high_price`/`low_price`/
`close_price` -> `high`/`low`/`close`, oldest-first as returned), and appends
one JSON line per symbol to `--build-file` (default
`data/.entries_candidates_build.jsonl`, gitignored scratch state -- not part
of the durable data/ contract synced to bot-state). A symbol with any
`interpolated: true` bar is skipped and logged (`candidate_bars_interpolated`)
rather than appended, matching the existing interpolated-bars rule elsewhere
in this skill. Prints nothing to stdout on success (silent, since this runs
many times per cycle) -- check exit code / the log for problems.

**--finalize** (run once, after all batches are appended): reads every line
of `--build-file`, cross-references `data/candidates.json` (sector, atr14)
and `data/pending_entries.json` (pending dual-cross state) by symbol, drops
any symbol already open in `data/positions.json` (defense in depth --
section 5 shouldn't have fetched those in the first place), and prints the
final `{"candidates": [...], "open_positions": []}` payload to stdout, ready
to pipe straight into `check_hourly_signals.py`. Deletes `--build-file`
afterward so a stale partial build can never silently leak into next cycle.

A cycle that dies partway through the append phase (crash, timeout) leaves
`--build-file` behind rather than losing the fetched work -- rerunning
--finalize picks up whatever was appended before the interruption. A fresh
cycle should not reuse a leftover build file from a prior, unrelated cycle
though: --finalize's first action is to fail loudly if `--build-file` looks
older than `--max-build-file-age-minutes` (default 120) rather than silently
finalizing stale data -- delete it and start over in that case.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.logging_utils import EventLogger
from lib.state import StateStore

DEFAULT_BUILD_FILE = "data/.entries_candidates_build.jsonl"


def _load_input(input_arg: str) -> dict:
    if input_arg == "-":
        return json.load(sys.stdin)
    with open(input_arg) as f:
        return json.load(f)


def _reshape_bars(raw_bars: list[dict]) -> tuple[list[dict], bool]:
    """Returns (bars, any_interpolated)."""
    bars = []
    any_interpolated = False
    for b in raw_bars:
        if b.get("interpolated"):
            any_interpolated = True
        bars.append(
            {
                "begins_at": b["begins_at"],
                "high": float(b["high_price"]),
                "low": float(b["low_price"]),
                "close": float(b["close_price"]),
            }
        )
    return bars, any_interpolated


def append_batch(payload: dict, build_file: Path, logger: EventLogger) -> list[str]:
    """Appends one raw get_equity_historicals response's symbols to
    build_file. Returns the list of symbols skipped for interpolated bars.
    """
    results = payload.get("data", payload).get("results", [])
    build_file.parent.mkdir(parents=True, exist_ok=True)
    skipped = []
    with build_file.open("a") as f:
        for result in results:
            symbol = result["symbol"]
            bars, any_interpolated = _reshape_bars(result.get("bars", []))
            if any_interpolated:
                skipped.append(symbol)
                continue
            f.write(json.dumps({"symbol": symbol, "bars": bars}) + "\n")
    if skipped:
        logger.log("candidate_bars_interpolated", symbols=skipped)
    return skipped


def finalize(
    build_file: Path,
    store: StateStore,
    logger: EventLogger,
    max_age_minutes: float,
    candidates_store: StateStore | None = None,
) -> dict:
    if not build_file.exists():
        return {"candidates": [], "open_positions": []}

    age_minutes = (time.time() - build_file.stat().st_mtime) / 60
    if age_minutes > max_age_minutes:
        build_file.unlink()
        raise RuntimeError(
            f"{build_file} is {age_minutes:.0f} minutes old (max {max_age_minutes}) -- "
            "looks like a stale leftover from an earlier, unrelated cycle. Deleted it; "
            "rerun --append for this cycle's batches before finalizing."
        )

    # candidates_store defaults to `store` itself (the original, single-track
    # behavior) -- the daily-stochastic-check track passes a SEPARATE store
    # pointed at the shared data/candidates.json (2026-08-13: that track's
    # own positions/pending state lives under data/daily/, but it reuses the
    # hourly track's candidate list rather than screening independently, at
    # the user's explicit choice) so candidates.json is read from one place
    # while pending_entries.json/positions.json are read from another.
    candidates_store = candidates_store or store
    candidates_by_symbol = {c["symbol"]: c for c in candidates_store.load_candidates()}
    pending_by_symbol = store.load_pending_entries()
    # A symbol already open OR already queued for a next-open fill (2026-08-14,
    # daily-stochastic-check's pending_fills.json -- see scripts/
    # queue_paper_fill.py) must not be re-evaluated as a fresh candidate;
    # load_pending_fills() is a no-op read (empty dict) for the hourly track,
    # which never writes that file, so this is safe to always include.
    open_symbols = set(store.load_positions().keys()) | set(store.load_pending_fills().keys())

    seen: dict[str, dict] = {}
    with build_file.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            seen[row["symbol"]] = row["bars"]

    candidates = []
    dropped_open = []
    for symbol, bars in seen.items():
        if symbol in open_symbols:
            dropped_open.append(symbol)
            continue
        c = candidates_by_symbol.get(symbol, {})
        entry = {
            "symbol": symbol,
            "sector": c.get("sector"),
            "atr14": c.get("atr14"),
            "bars": bars,
        }
        pending = pending_by_symbol.get(symbol)
        if pending is not None:
            entry["pending"] = pending
        candidates.append(entry)

    if dropped_open:
        logger.log("candidate_payload_dropped_open_position", symbols=dropped_open)

    build_file.unlink()
    return {"candidates": candidates, "open_positions": []}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--append", action="store_true", help="Append one fetched batch's symbols to the build file.")
    mode.add_argument("--finalize", action="store_true", help="Emit the final combined candidates payload.")
    parser.add_argument("--input", default="-", help="For --append: JSON input file, or '-' for stdin (default).")
    parser.add_argument("--build-file", default=DEFAULT_BUILD_FILE)
    parser.add_argument("--data-dir", default="data")
    parser.add_argument(
        "--candidates-data-dir",
        default=None,
        help="For --finalize: read candidates.json from this data dir instead of --data-dir "
        "(pending_entries.json/positions.json still come from --data-dir). Defaults to --data-dir. "
        "Used by the daily-stochastic-check track to share the hourly track's candidate list "
        "while keeping its own position/pending state separate.",
    )
    parser.add_argument("--max-build-file-age-minutes", type=float, default=120.0)
    args = parser.parse_args()

    logger = EventLogger(args.data_dir)
    build_file = Path(args.build_file)

    if args.append:
        payload = _load_input(args.input)
        skipped = append_batch(payload, build_file, logger)
        if skipped:
            print(f"skipped (interpolated bars): {', '.join(skipped)}", file=sys.stderr)
        return

    store = StateStore(args.data_dir)
    candidates_store = StateStore(args.candidates_data_dir) if args.candidates_data_dir else None
    result = finalize(build_file, store, logger, args.max_build_file_age_minutes, candidates_store=candidates_store)
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
