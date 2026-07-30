from datetime import date

from lib.catalysts import (
    earnings_exit_date,
    filter_earnings_entry_blocked,
    is_entry_blocked_day,
    is_forced_exit_day,
    next_forced_exit_date,
)


def test_earnings_exit_date_bmo_is_day_before():
    assert earnings_exit_date(date(2026, 8, 6), "am") == date(2026, 8, 5)


def test_earnings_exit_date_amc_is_same_day():
    assert earnings_exit_date(date(2026, 8, 6), "pm") == date(2026, 8, 6)


def test_earnings_exit_date_unknown_timing_treated_as_bmo():
    assert earnings_exit_date(date(2026, 8, 6), "unknown") == date(2026, 8, 5)


def test_next_forced_exit_date_picks_nearest_upcoming():
    reports = [
        {"date": "2026-04-15", "timing": "am"},  # past, ignored
        {"date": "2026-10-20", "timing": "pm"},  # further out
        {"date": "2026-08-06", "timing": "am"},  # nearest upcoming -> exit 08-05
    ]
    assert next_forced_exit_date(reports, as_of=date(2026, 7, 29)) == date(2026, 8, 5)


def test_next_forced_exit_date_none_when_all_past():
    reports = [{"date": "2026-01-10", "timing": "am"}]
    assert next_forced_exit_date(reports, as_of=date(2026, 7, 29)) is None


def test_next_forced_exit_date_none_when_empty():
    assert next_forced_exit_date([], as_of=date(2026, 7, 29)) is None


def test_next_forced_exit_date_accepts_full_timestamps():
    reports = [{"date": "2026-08-01T21:00:00Z", "timing": "pm"}]
    assert next_forced_exit_date(reports, as_of=date(2026, 7, 29)) == date(2026, 8, 1)


def test_next_forced_exit_date_accepts_legacy_bare_date_strings():
    # Backward compat with earnings data persisted before timing existed --
    # treated as conservative "am"/BMO.
    reports = ["2026-08-06"]
    assert next_forced_exit_date(reports, as_of=date(2026, 7, 29)) == date(2026, 8, 5)


def test_is_forced_exit_day_true_on_bmo_exit_date():
    reports = [{"date": "2026-08-06", "timing": "am"}]
    assert is_forced_exit_day(reports, as_of=date(2026, 8, 5)) is True


def test_is_forced_exit_day_false_day_before_bmo_exit_date():
    reports = [{"date": "2026-08-06", "timing": "am"}]
    assert is_forced_exit_day(reports, as_of=date(2026, 8, 4)) is False


def test_is_forced_exit_day_true_on_amc_report_day_itself():
    reports = [{"date": "2026-08-06", "timing": "pm"}]
    assert is_forced_exit_day(reports, as_of=date(2026, 8, 6)) is True


def test_is_forced_exit_day_false_day_before_amc_report_day():
    # This is the whole point of the fix: an AMC report shouldn't force an
    # exit a day early and forfeit the run-up into the report.
    reports = [{"date": "2026-08-06", "timing": "pm"}]
    assert is_forced_exit_day(reports, as_of=date(2026, 8, 5)) is False


def test_is_forced_exit_day_still_true_once_report_date_itself_has_arrived():
    # Mirrors the pre-existing "upcoming" semantics (report_date >= as_of):
    # the report date itself still counts as upcoming, so a BMO exit_date
    # computed the day before remains valid right up through report day.
    reports = [{"date": "2026-08-06", "timing": "am"}]
    assert is_forced_exit_day(reports, as_of=date(2026, 8, 6)) is True


def test_is_forced_exit_day_false_when_no_upcoming_report():
    reports = [{"date": "2026-01-01", "timing": "am"}]
    assert is_forced_exit_day(reports, as_of=date(2026, 7, 29)) is False


def test_is_entry_blocked_day_true_exactly_on_exit_date():
    reports = [{"date": "2026-08-06", "timing": "am"}]
    assert is_entry_blocked_day(reports, as_of=date(2026, 8, 5)) is True


def test_is_entry_blocked_day_false_two_days_before():
    # Days before the exit_date are NOT blocked -- entries there capture the
    # run-up into the report, which is exactly what this rule change is for.
    reports = [{"date": "2026-08-06", "timing": "am"}]
    assert is_entry_blocked_day(reports, as_of=date(2026, 8, 3)) is False


def test_is_entry_blocked_day_false_once_past_exit_date():
    reports = [{"date": "2026-08-06", "timing": "am"}]
    assert is_entry_blocked_day(reports, as_of=date(2026, 8, 6)) is False


def test_filter_earnings_entry_blocked_splits_correctly():
    candidates = [
        {"symbol": "AAA"},  # exit_date is today -> excluded, too_close
        {"symbol": "BBB"},  # earnings far out -> kept
        {"symbol": "CCC"},  # checked, no upcoming report -> kept
        {"symbol": "DDD"},  # not in earnings_by_symbol at all -> excluded, unknown
    ]
    earnings_by_symbol = {
        "AAA": [{"date": "2026-07-30", "timing": "am"}],  # exit_date = 2026-07-29 = as_of
        "BBB": [{"date": "2026-08-28", "timing": "am"}],
        "CCC": [],
    }

    kept, excluded = filter_earnings_entry_blocked(candidates, earnings_by_symbol, as_of=date(2026, 7, 29))

    assert [c["symbol"] for c in kept] == ["BBB", "CCC"]
    excluded_by_symbol = {c["symbol"]: c["earnings_exclusion_reason"] for c in excluded}
    assert excluded_by_symbol == {"AAA": "too_close", "DDD": "unknown"}


def test_filter_earnings_entry_blocked_preserves_other_fields():
    candidates = [{"symbol": "AAA", "sector": "Tech", "last_price": 100}]
    kept, excluded = filter_earnings_entry_blocked(candidates, {"AAA": []}, as_of=date(2026, 7, 29))
    assert kept == [{"symbol": "AAA", "sector": "Tech", "last_price": 100}]


def test_filter_earnings_entry_blocked_all_kept_when_no_exclusions():
    candidates = [{"symbol": "AAA"}, {"symbol": "BBB"}]
    kept, excluded = filter_earnings_entry_blocked(
        candidates, {"AAA": [], "BBB": [{"date": "2099-01-01", "timing": "am"}]}, as_of=date(2026, 7, 29)
    )
    assert len(kept) == 2
    assert excluded == []
