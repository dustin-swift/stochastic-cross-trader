import json
from datetime import datetime, timezone

from lib.logging_utils import EventLogger


def test_log_writes_one_jsonl_line(tmp_path):
    fixed_now = datetime(2026, 7, 29, 15, 0, 0, tzinfo=timezone.utc)
    logger = EventLogger(tmp_path, now=lambda: fixed_now)

    logger.log("signal_check", symbol="AAPL", entry=False)

    log_file = tmp_path / "logs" / "2026-07-29.jsonl"
    assert log_file.exists()
    lines = log_file.read_text().splitlines()
    assert len(lines) == 1

    record = json.loads(lines[0])
    assert record["event"] == "signal_check"
    assert record["symbol"] == "AAPL"
    assert record["entry"] is False
    assert record["timestamp"] == fixed_now.isoformat()


def test_log_appends_multiple_events_same_day(tmp_path):
    fixed_now = datetime(2026, 7, 29, 15, 0, 0, tzinfo=timezone.utc)
    logger = EventLogger(tmp_path, now=lambda: fixed_now)

    logger.log("event_a")
    logger.log("event_b")

    log_file = tmp_path / "logs" / "2026-07-29.jsonl"
    lines = log_file.read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["event"] == "event_a"
    assert json.loads(lines[1])["event"] == "event_b"


def test_log_separates_by_day(tmp_path):
    day1 = datetime(2026, 7, 29, 15, 0, 0, tzinfo=timezone.utc)
    day2 = datetime(2026, 7, 30, 15, 0, 0, tzinfo=timezone.utc)
    calls = iter([day1, day2])
    logger = EventLogger(tmp_path, now=lambda: next(calls))

    logger.log("event_a")
    logger.log("event_b")

    assert (tmp_path / "logs" / "2026-07-29.jsonl").exists()
    assert (tmp_path / "logs" / "2026-07-30.jsonl").exists()


def test_log_returns_the_record(tmp_path):
    logger = EventLogger(tmp_path)
    record = logger.log("order_placed", symbol="AAPL", qty=1.5)
    assert record["event"] == "order_placed"
    assert record["qty"] == 1.5
