import json

from lib.state import StateStore, has_open_slot, open_slot_count


def test_load_defaults_when_no_files(tmp_path):
    store = StateStore(tmp_path)
    assert store.load_candidates() == []
    assert store.load_positions() == {}
    assert store.load_daily_pnl() == {}


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
