import json

import pytest

from lib.state import StateStore, close_trade_record, has_open_slot, open_slot_count


def test_load_defaults_when_no_files(tmp_path):
    store = StateStore(tmp_path)
    assert store.load_candidates() == []
    assert store.load_positions() == {}
    assert store.load_daily_pnl() == {}
    assert store.load_trade_history() == []
    assert store.load_pending_entries() == {}
    assert store.load_pending_fills() == {}
    assert store.load_last_cycle_at() is None
    assert store.load_watchlist() == {}


def test_save_and_load_candidates_roundtrip(tmp_path):
    store = StateStore(tmp_path)
    candidates = [{"symbol": "AAPL", "sector": "Tech"}, {"symbol": "XOM", "sector": "Energy"}]
    store.save_candidates(candidates)
    assert store.load_candidates() == candidates


def test_save_and_load_pending_entries_roundtrip(tmp_path):
    store = StateStore(tmp_path)
    pending = {"AAPL": {"k_at_cross": 22.5}, "MSFT": {"k_at_cross": 20.1}}
    store.save_pending_entries(pending)
    assert store.load_pending_entries() == pending


def test_save_and_load_pending_fills_roundtrip(tmp_path):
    store = StateStore(tmp_path)
    pending = {"AAPL": {"atr14": 3.2, "signal_date": "2026-08-14"}}
    store.save_pending_fills(pending)
    assert store.load_pending_fills() == pending


def test_save_and_load_last_cycle_at_roundtrip(tmp_path):
    store = StateStore(tmp_path)
    store.save_last_cycle_at("2026-08-04T15:47:00+00:00")
    assert store.load_last_cycle_at() == "2026-08-04T15:47:00+00:00"


def test_save_and_load_watchlist_roundtrip(tmp_path):
    store = StateStore(tmp_path)
    watchlist = {
        "AAPL": {
            "breakout_date": "2026-01-05",
            "breakout_level": 97.32,
            "retest_seen": True,
            "retest_low": 94.1,
            "failed": False,
            "eligible_for_entry": True,
        }
    }
    store.save_watchlist(watchlist)
    assert store.load_watchlist() == watchlist


def test_watchlist_is_a_distinct_file_from_candidates(tmp_path):
    store = StateStore(tmp_path)
    store.save_watchlist({"AAPL": {"breakout_level": 100.0}})
    store.save_candidates([{"symbol": "AAPL"}])
    assert store.watchlist_path != store.candidates_path
    assert store.load_watchlist() == {"AAPL": {"breakout_level": 100.0}}
    assert store.load_candidates() == [{"symbol": "AAPL"}]


def test_save_and_load_positions_roundtrip(tmp_path):
    store = StateStore(tmp_path)
    positions = {
        "AAPL": {
            "entry_price": 200.0,
            "qty": 0.5,
            "entry_time": "2026-07-29T14:30:00Z",
            "entry_order_id": "order-1",
            "stop_order_id": "order-2",
            "stop_price": 195.0,
        }
    }
    store.save_positions(positions)
    assert store.load_positions() == positions


def test_save_is_atomic_no_leftover_tmp_files(tmp_path):
    store = StateStore(tmp_path)
    store.save_positions({"AAPL": {"qty": 1}})
    leftovers = [p for p in tmp_path.iterdir() if p.suffix == ".tmp"]
    assert leftovers == []
    assert (tmp_path / "positions.json").exists()


def test_save_produces_valid_json_file(tmp_path):
    store = StateStore(tmp_path)
    store.save_candidates([{"symbol": "AAPL"}])
    with (tmp_path / "candidates.json").open() as f:
        data = json.load(f)
    assert data == [{"symbol": "AAPL"}]


def test_append_trade_history_accumulates(tmp_path):
    store = StateStore(tmp_path)
    store.append_trade_history({"symbol": "AAPL", "pnl_usd": 5.0})
    store.append_trade_history({"symbol": "MSFT", "pnl_usd": -2.0})
    history = store.load_trade_history()
    assert [r["symbol"] for r in history] == ["AAPL", "MSFT"]


def test_close_trade_record_computes_pnl():
    position = {
        "entry_price": 100.0,
        "qty": 2.0,
        "entry_time": "2026-07-29T14:30:00Z",
        "entry_order_id": "entry-1",
        "stop_price": 95.0,
        "stop_order_id": "stop-1",
        "entry_k": 24.1,
        "entry_d": 19.8,
        "entry_prev_k": 18.6,
        "entry_prev_d": 15.2,
    }
    record = close_trade_record(
        symbol="AAPL",
        position=position,
        exit_price=110.0,
        exit_time="2026-07-30T15:00:00Z",
        exit_order_id="exit-1",
        exit_reason="signal_exit",
        closed_at="2026-07-30T15:00:05Z",
        exit_k=41.0,
        exit_d=45.2,
        exit_prev_k=48.3,
        exit_prev_d=44.7,
    )
    assert record["pnl_usd"] == 20.0
    assert round(record["pnl_pct"], 4) == 10.0
    assert record["symbol"] == "AAPL"
    assert record["entry_order_id"] == "entry-1"
    assert record["exit_order_id"] == "exit-1"
    assert record["exit_reason"] == "signal_exit"
    # Stochastic detail (2026-08-05, post-trade review data): entry-side
    # comes from `position` (written by the skill at entry time), exit-side
    # is passed in directly by the caller.
    assert record["entry_k"] == 24.1
    assert record["entry_d"] == 19.8
    assert record["entry_prev_k"] == 18.6
    assert record["entry_prev_d"] == 15.2
    assert record["exit_k"] == 41.0
    assert record["exit_d"] == 45.2
    assert record["exit_prev_k"] == 48.3
    assert record["exit_prev_d"] == 44.7


def test_close_trade_record_handles_unknown_exit_price():
    position = {"entry_price": 100.0, "qty": 1.0}
    record = close_trade_record(
        symbol="AAPL",
        position=position,
        exit_price=None,
        exit_time="2026-07-30T15:00:00Z",
        exit_order_id=None,
        exit_reason="signal_exit",
        closed_at="2026-07-30T15:00:05Z",
    )
    assert record["exit_price"] is None
    assert record["pnl_usd"] is None
    assert record["pnl_pct"] is None


def test_close_trade_record_stochastic_fields_default_to_none():
    # A position/stop_out written before this feature existed (or one with
    # no fresh oscillator reading, e.g. a stop-out) must not error -- every
    # stochastic field simply comes back None, no migration needed.
    position = {"entry_price": 100.0, "qty": 1.0}
    record = close_trade_record(
        symbol="AAPL",
        position=position,
        exit_price=105.0,
        exit_time="2026-07-30T15:00:00Z",
        exit_order_id="exit-1",
        exit_reason="stop_out",
        closed_at="2026-07-30T15:00:05Z",
    )
    assert record["entry_k"] is None
    assert record["entry_d"] is None
    assert record["entry_prev_k"] is None
    assert record["entry_prev_d"] is None
    assert record["exit_k"] is None
    assert record["exit_d"] is None
    assert record["exit_prev_k"] is None
    assert record["exit_prev_d"] is None


def test_close_trade_record_accepts_shares_key_for_ma_pullback_positions():
    # Bug fixed 2026-08-21, caught live on the MA Pullback agent's first
    # stop-out: this function is shared verbatim by both trading systems,
    # but the MA agent's positions.json uses "shares", not "qty" (see
    # lib.ma_signals.evaluate_entry) -- hardcoding "qty" raised a bare
    # KeyError. The output record still normalizes to "qty" either way,
    # since that's the field name every downstream consumer (the dashboard,
    # trade_history.json readers) expects.
    position = {
        "entry_price": 44.55,
        "shares": 2,
        "stop_price": 43.07,
        "target_1": 46.76,
        "breakout_level": 43.89,
    }
    record = close_trade_record(
        symbol="FRO",
        position=position,
        exit_price=43.0,
        exit_time="2026-08-21T20:00:00Z",
        exit_order_id="exit-1",
        exit_reason="stop_hit",
        closed_at="2026-08-21T20:00:05Z",
    )
    assert record["qty"] == 2
    assert round(record["pnl_usd"], 2) == -3.10
    assert "shares" not in record


def test_close_trade_record_prefers_qty_when_both_present():
    position = {"entry_price": 100.0, "qty": 5, "shares": 999}
    record = close_trade_record(
        symbol="AAPL",
        position=position,
        exit_price=110.0,
        exit_time="2026-07-30T15:00:00Z",
        exit_order_id="exit-1",
        exit_reason="signal_exit",
        closed_at="2026-07-30T15:00:05Z",
    )
    assert record["qty"] == 5


def test_close_trade_record_neither_qty_nor_shares_raises():
    position = {"entry_price": 100.0}
    with pytest.raises(KeyError):
        close_trade_record(
            symbol="AAPL",
            position=position,
            exit_price=110.0,
            exit_time="2026-07-30T15:00:00Z",
            exit_order_id="exit-1",
            exit_reason="signal_exit",
            closed_at="2026-07-30T15:00:05Z",
        )


def test_has_open_slot():
    assert has_open_slot({}, max_positions=10) is True
    positions = {f"SYM{i}": {} for i in range(10)}
    assert has_open_slot(positions, max_positions=10) is False


def test_open_slot_count():
    assert open_slot_count({}, max_positions=10) == 10
    positions = {f"SYM{i}": {} for i in range(7)}
    assert open_slot_count(positions, max_positions=10) == 3
    positions_over = {f"SYM{i}": {} for i in range(12)}
    assert open_slot_count(positions_over, max_positions=10) == 0
