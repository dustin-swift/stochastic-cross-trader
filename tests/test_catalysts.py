from datetime import date

import pytest

from lib.catalysts import (
    days_until_next_earnings,
    filter_earnings_too_close,
    is_too_close_to_earnings,
)


def test_days_until_next_earnings_basic():
    assert days_until_next_earnings(["2026-08-01"], as_of=date(2026, 7, 29)) == 3


def test_days_until_next_earnings_today_is_zero():
    assert days_until_next_earnings(["2026-07-29"], as_of=date(2026, 7, 29)) == 0


def test_days_until_next_earnings_picks_nearest_future_date():
    # Mix of past (should be ignored) and future dates -- nearest future wins.
    dates = ["2026-04-15", "2026-01-10", "2026-08-05", "2026-10-20"]
    assert days_until_next_earnings(dates, as_of=date(2026, 7, 29)) == 7


def test_days_until_next_earnings_none_when_all_past():
    dates = ["2026-01-10", "2026-04-15"]
    assert days_until_next_earnings(dates, as_of=date(2026, 7, 29)) is None


def test_days_until_next_earnings_none_when_empty():
    assert days_until_next_earnings([], as_of=date(2026, 7, 29)) is None


def test_days_until_next_earnings_accepts_full_timestamps():
    assert days_until_next_earnings(["2026-08-01T21:00:00Z"], as_of=date(2026, 7, 29)) == 3


def test_is_too_close_to_earnings_true_within_window():
    assert is_too_close_to_earnings(["2026-08-01"], as_of=date(2026, 7, 29), max_days=5) is True


def test_is_too_close_to_earnings_false_outside_window():
    assert is_too_close_to_earnings(["2026-08-10"], as_of=date(2026, 7, 29), max_days=5) is False


def test_is_too_close_to_earnings_true_at_exact_boundary():
    assert is_too_close_to_earnings(["2026-08-03"], as_of=date(2026, 7, 29), max_days=5) is True


def test_is_too_close_to_earnings_false_when_no_upcoming_report():
    assert is_too_close_to_earnings(["2026-01-01"], as_of=date(2026, 7, 29), max_days=5) is False


def test_filter_earnings_too_close_splits_correctly():
    candidates = [
        {"symbol": "AAA"},  # earnings in 3 days -> excluded, too_close
        {"symbol": "BBB"},  # earnings in 30 days -> kept
        {"symbol": "CCC"},  # checked, no upcoming report -> kept
        {"symbol": "DDD"},  # not in earnings_by_symbol at all -> excluded, unknown
    ]
    earnings_by_symbol = {
        "AAA": ["2026-08-01"],
        "BBB": ["2026-08-28"],
        "CCC": [],
    }

    kept, excluded = filter_earnings_too_close(candidates, earnings_by_symbol, as_of=date(2026, 7, 29), max_days=5)

    assert [c["symbol"] for c in kept] == ["BBB", "CCC"]
    excluded_by_symbol = {c["symbol"]: c["earnings_exclusion_reason"] for c in excluded}
    assert excluded_by_symbol == {"AAA": "too_close", "DDD": "unknown"}


def test_filter_earnings_too_close_preserves_other_fields():
    candidates = [{"symbol": "AAA", "sector": "Tech", "last_price": 100}]
    kept, excluded = filter_earnings_too_close(candidates, {"AAA": []}, as_of=date(2026, 7, 29), max_days=5)
    assert kept == [{"symbol": "AAA", "sector": "Tech", "last_price": 100}]


def test_filter_earnings_too_close_all_kept_when_no_map_entries_missing():
    candidates = [{"symbol": "AAA"}, {"symbol": "BBB"}]
    kept, excluded = filter_earnings_too_close(
        candidates, {"AAA": [], "BBB": ["2099-01-01"]}, as_of=date(2026, 7, 29), max_days=5
    )
    assert len(kept) == 2
    assert excluded == []
