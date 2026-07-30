import json

from lib.state import StateStore, close_trade_record, has_open_slot, open_slot_count


def test_load_defaults_when_no_files(tmp_path):
    store = StateStore(tmp_path)
    assert store.load_candidates() == []
    assert store.load_positions() == {}
    assert store.load_daily_pnl() == {}
    assert store.load_trade_history() == []


def test_save_and_load_candidates_roundtrip(tmp_path):
    store = StateStore(tmp_path)
    candidates = [{"symbol": "AAPL", "sector": "Tech"}, {"symbol": "XOM", "sector": "Energy"}]
    store.save_candidates(candidates)
    assert store.load_candidates() == candidates


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
    }
    record = close_trade_record(
        symbol="AAPL",
        position=position,
        exit_price=110.0,
        exit_time="2026-07-30T15:00:00Z",
        exit_order_id="exit-1",
        exit_reason="signal_exit",
        closed_at="2026-07-30T15:00:05Z",
    )
    assert record["pnl_usd"] == 20.0
    assert round(record["pnl_pct"], 4) == 10.0
    assert record["symbol"] == "AAPL"
    assert record["entry_order_id"] == "entry-1"
    assert record["exit_order_id"] == "exit-1"
    assert record["exit_reason"] == "signal_exit"


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
