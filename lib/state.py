"""JSON-backed state: today's candidate list, open positions, and daily P&L.

Files live under a data directory (default `data/`, gitignored). Writes are
atomic (write to a temp file in the same directory, then os.replace) since a
scheduled cloud agent run could in principle overlap with a manual run.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp_path, path)
    except BaseException:
        Path(tmp_path).unlink(missing_ok=True)
        raise


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open() as f:
        return json.load(f)


class StateStore:
    """All strategy state for one data directory."""

    def __init__(self, data_dir: str | Path = "data"):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)

    # -- candidates -----------------------------------------------------
    @property
    def candidates_path(self) -> Path:
        return self.data_dir / "candidates.json"

    def load_candidates(self) -> list[dict]:
        return _read_json(self.candidates_path, default=[])

    def save_candidates(self, candidates: list[dict]) -> None:
        _atomic_write_json(self.candidates_path, candidates)

    # -- positions --------------------------------------------------------
    @property
    def positions_path(self) -> Path:
        return self.data_dir / "positions.json"

    def load_positions(self) -> dict[str, dict]:
        return _read_json(self.positions_path, default={})

    def save_positions(self, positions: dict[str, dict]) -> None:
        _atomic_write_json(self.positions_path, positions)

    # -- pending entries (spec §3, 2026-08-04 dual-cross revision) --------
    # {symbol: {"k_at_cross": float}} for candidates where %K has crossed
    # above the oversold threshold and the setup is waiting on %D to also
    # cross before check_hourly_signals.py fires an entry -- see
    # lib.signals.advance_pending_entry. Intraday-only state: the daily
    # screen clears this each morning (a pending setup from yesterday's
    # candidate list/price context shouldn't silently carry into today).
    @property
    def pending_entries_path(self) -> Path:
        return self.data_dir / "pending_entries.json"

    def load_pending_entries(self) -> dict[str, dict]:
        return _read_json(self.pending_entries_path, default={})

    def save_pending_entries(self, pending_by_symbol: dict[str, dict]) -> None:
        _atomic_write_json(self.pending_entries_path, pending_by_symbol)

    # -- daily P&L ----------------------------------------------------------
    @property
    def daily_pnl_path(self) -> Path:
        return self.data_dir / "daily_pnl.json"

    def load_daily_pnl(self) -> dict[str, dict]:
        return _read_json(self.daily_pnl_path, default={})

    def save_daily_pnl(self, pnl_by_date: dict[str, dict]) -> None:
        _atomic_write_json(self.daily_pnl_path, pnl_by_date)

    # -- closed-trade history -------------------------------------------
    # positions.json only ever tracks OPEN positions -- once a position
    # closes (stop-out, signal exit, or earnings-forced exit) it's removed
    # from there. This is the only durable record of what actually happened
    # to a closed trade, so the dashboard (and any P&L review) reads from
    # here, not from positions.json.
    @property
    def trade_history_path(self) -> Path:
        return self.data_dir / "trade_history.json"

    def load_trade_history(self) -> list[dict]:
        return _read_json(self.trade_history_path, default=[])

    def append_trade_history(self, record: dict) -> None:
        history = self.load_trade_history()
        history.append(record)
        _atomic_write_json(self.trade_history_path, history)


def close_trade_record(
    symbol: str,
    position: dict,
    exit_price: float | None,
    exit_time: str,
    exit_order_id: str | None,
    exit_reason: str,
    closed_at: str,
) -> dict:
    """Build a closed-trade record from an open position dict + exit info.

    `exit_price` may be None (e.g. a sell order that never confirmed a fill
    before this cycle had to move on) -- pnl fields are then also None rather
    than a fabricated number. Pure function so the pnl math is unit-testable
    without touching the filesystem or a broker.
    """
    qty = position["qty"]
    entry_price = position["entry_price"]
    pnl_usd = None
    pnl_pct = None
    if exit_price is not None:
        pnl_usd = (exit_price - entry_price) * qty
        pnl_pct = (exit_price / entry_price - 1) * 100
    return {
        "symbol": symbol,
        "qty": qty,
        "entry_price": entry_price,
        "entry_time": position.get("entry_time"),
        "entry_order_id": position.get("entry_order_id"),
        "stop_price": position.get("stop_price"),
        "stop_order_id": position.get("stop_order_id"),
        "exit_price": exit_price,
        "exit_time": exit_time,
        "exit_order_id": exit_order_id,
        "exit_reason": exit_reason,
        "pnl_usd": pnl_usd,
        "pnl_pct": pnl_pct,
        "closed_at": closed_at,
    }


def has_open_slot(positions: dict[str, dict], max_positions: int) -> bool:
    return len(positions) < max_positions


def open_slot_count(positions: dict[str, dict], max_positions: int) -> int:
    return max(0, max_positions - len(positions))
