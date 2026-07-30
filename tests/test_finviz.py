import os
from datetime import date, datetime

import pytest

from providers.finviz import check_freshness, load_universe

VALID_CSV = "Ticker,Sector,Price\nAAPL,Electronic Technology,210.5\nJPM,Finance,349.3\n"


def _write(tmp_path, content, name="finviz_export.csv"):
    path = tmp_path / name
    path.write_text(content)
    return path


def _set_mtime(path, when: datetime):
    ts = when.timestamp()
    os.utime(path, (ts, ts))


# -- load_universe -----------------------------------------------------


def test_load_universe_valid(tmp_path):
    path = _write(tmp_path, VALID_CSV)
    rows = load_universe(str(path))
    assert rows == [
        {"Ticker": "AAPL", "Sector": "Electronic Technology", "Price": "210.5"},
        {"Ticker": "JPM", "Sector": "Finance", "Price": "349.3"},
    ]


def test_load_universe_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_universe(str(tmp_path / "does_not_exist.csv"))


def test_load_universe_missing_columns(tmp_path):
    path = _write(tmp_path, "Ticker,Price\nAAPL,210.5\n")
    with pytest.raises(ValueError, match="Sector"):
        load_universe(str(path))


def test_load_universe_empty_file_missing_columns(tmp_path):
    path = _write(tmp_path, "")
    with pytest.raises(ValueError):
        load_universe(str(path))


# -- check_freshness (trading-day-aware) --------------------------------


def test_check_freshness_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        check_freshness(str(tmp_path / "does_not_exist.csv"), today=date(2026, 7, 29))


def test_check_freshness_same_day_export_is_fresh(tmp_path):
    path = _write(tmp_path, VALID_CSV)
    _set_mtime(path, datetime(2026, 7, 29, 9, 0))  # Wednesday
    check_freshness(str(path), today=date(2026, 7, 29))  # no raise


@pytest.mark.parametrize(
    "today",
    [
        date(2026, 8, 1),  # Saturday
        date(2026, 8, 2),  # Sunday
        date(2026, 8, 3),  # Monday
    ],
)
def test_friday_export_valid_through_the_weekend_and_monday(tmp_path, today):
    path = _write(tmp_path, VALID_CSV)
    _set_mtime(path, datetime(2026, 7, 31, 16, 0))  # Friday
    check_freshness(str(path), today=today)  # no raise


def test_friday_export_stale_by_tuesday(tmp_path):
    path = _write(tmp_path, VALID_CSV)
    _set_mtime(path, datetime(2026, 7, 31, 16, 0))  # Friday
    with pytest.raises(ValueError, match="stale"):
        check_freshness(str(path), today=date(2026, 8, 4))  # Tuesday


def test_stale_midweek_export(tmp_path):
    path = _write(tmp_path, VALID_CSV)
    _set_mtime(path, datetime(2026, 7, 27, 9, 0))  # Monday
    with pytest.raises(ValueError, match="stale"):
        check_freshness(str(path), today=date(2026, 7, 29))  # Wednesday - needs Tuesday+
