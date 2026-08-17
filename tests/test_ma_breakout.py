import pandas as pd
import pytest

from lib.ma_breakout import detect_breakout, update_watchlist_entry


def _bars(highs, lows, closes, volumes=None, start="2024-01-01"):
    n = len(highs)
    dates = pd.bdate_range(start, periods=n).strftime("%Y-%m-%d").tolist()
    volumes = volumes if volumes is not None else [1_000_000.0] * n
    return pd.DataFrame({
        "date": dates,
        "high": [float(x) for x in highs],
        "low": [float(x) for x in lows],
        "close": [float(x) for x in closes],
        "volume": [float(x) for x in volumes],
    })


def _cfg(**overrides):
    breakout = {
        "lookback_days": 5,
        "max_breakout_age_days": 10,
        "failed_breakout_depth": 0.10,
        "min_separation_days": 2,
        "min_extension_pct": 0.02,
    }
    breakout.update(overrides)
    return {"breakout": breakout}


# --- detect_breakout: breakout_level is the PRIOR high, never today's own high ---

def test_detect_breakout_uses_prior_high_not_todays_high():
    # 25 flat bars at high=100, then a 26th bar with a much higher high (110).
    highs = [100.0] * 25 + [110.0]
    lows = [95.0] * 26
    closes = [98.0] * 26
    bars = _bars(highs, lows, closes)

    level = detect_breakout(bars, lookback=20)

    # The prior 20-bar high (100), NOT today's own high (110) -- this is the
    # single biggest correction the spec documents.
    assert level == 100.0


def test_detect_breakout_no_fresh_breakout_when_today_below_prior_high():
    highs = [100.0] * 25 + [99.0]
    lows = [95.0] * 26
    closes = [98.0] * 26
    bars = _bars(highs, lows, closes)

    assert detect_breakout(bars, lookback=20) is None


def test_detect_breakout_insufficient_history_returns_none():
    highs = [100.0] * 15
    lows = [95.0] * 15
    closes = [98.0] * 15
    bars = _bars(highs, lows, closes)

    assert detect_breakout(bars, lookback=20) is None


def test_update_watchlist_entry_records_prior_high_as_breakout_level():
    highs = [100.0] * 25 + [110.0]
    lows = [95.0] * 26
    closes = [98.0] * 25 + [105.0]
    bars = _bars(highs, lows, closes)

    entry, eligible = update_watchlist_entry(None, bars, _cfg(lookback_days=20))

    assert entry["breakout_level"] == 100.0
    assert eligible is False  # breakout day itself -- nothing to update yet


# --- retest requires separation + extension before counting ---

def _breakout_and_days(day_specs, lookback=5, **cfg_overrides):
    """day_specs: list of (high, low, close) tuples, starting with the
    pre-breakout warmup bars and continuing day by day through the breakout
    and beyond. Simulates calling update_watchlist_entry once per day, as
    the real daily scan would, growing the bars array each call and carrying
    the entry forward. Returns the list of (entry, eligible) results, one per
    day starting from the first day with enough history to evaluate.
    """
    highs = [h for h, l, c in day_specs]
    lows = [l for h, l, c in day_specs]
    closes = [c for h, l, c in day_specs]
    bars = _bars(highs, lows, closes)
    config = _cfg(lookback_days=lookback, **cfg_overrides)

    results = []
    entry = None
    min_len = lookback + 5
    for day in range(min_len, len(bars) + 1):
        entry, eligible = update_watchlist_entry(entry, bars.iloc[:day], config)
        results.append((entry, eligible))
    return results


def test_retest_requires_both_separation_and_extension():
    # 10 flat pre-breakout bars (high=100), then:
    #  day10: breakout (high=101 >= prior 5-bar high of 100) -> breakout_level=100.
    #    Note: day10's own high (101) stays inside every subsequent day's
    #    trailing window for the next several days, which caps how far
    #    later days' highs (and therefore closes) can go without triggering
    #    ANOTHER fresh breakout -- so this scenario keeps every bar's high
    #    below 101 and uses a small min_extension_pct to still exercise the
    #    separation requirement in isolation.
    #  day11 (days_since=1): close is already extended enough (0.3%) but NOT
    #    enough separation yet (min_separation_days=2) -- extension must
    #    stay unconfirmed, and a same-day dip to the level (low=99) must NOT
    #    be recorded as a retest.
    #  day12 (days_since=2): separation now satisfied AND still extended
    #    (0.6%) -> extension_confirmed flips True. No dip this bar.
    #  day13 (days_since=3): a genuine pullback (low=99.5) -- NOW counts as
    #    the retest, since extension is confirmed.
    pre = [(100.0, 95.0, 98.0)] * 10
    day_specs = pre + [
        (101.0, 100.0, 100.2),   # day10: breakout, close only 0.2% extended
        (100.8, 99.0, 100.3),    # day11: 0.3% extension, but no separation yet
        (100.9, 100.4, 100.6),   # day12: separation satisfied, 0.6% extension
        (100.9, 99.5, 100.0),    # day13: genuine retest touch
    ]
    results = _breakout_and_days(day_specs, lookback=5, min_separation_days=2, min_extension_pct=0.005)

    entry_day10, eligible_day10 = results[-4]
    entry_day11, eligible_day11 = results[-3]
    entry_day12, eligible_day12 = results[-2]
    entry_day13, eligible_day13 = results[-1]

    assert entry_day10["breakout_level"] == 100.0
    assert eligible_day10 is False

    # day11: extension NOT confirmed despite already exceeding 0.3% (separation
    # not met), so the dip to 99 must not register as a retest.
    assert entry_day11["extension_confirmed"] is False
    assert entry_day11["retest_seen"] is False
    assert eligible_day11 is False

    # day12: separation now satisfied and still extended -> confirmed, but no
    # dip occurred this bar, so still no retest yet.
    assert entry_day12["extension_confirmed"] is True
    assert entry_day12["retest_seen"] is False

    # day13: genuine retest, now that extension is confirmed.
    assert entry_day13["retest_seen"] is True
    assert entry_day13["retest_low"] == 99.5
    assert eligible_day13 is True


def test_v_shaped_no_separation_retest_is_rejected():
    # A same-candle-ish whipsaw: breakout, then an IMMEDIATE (next-day) dip
    # straight back through breakout_level, with no separation at all. Even
    # though the dip touches the level, it must not be counted as a retest.
    pre = [(100.0, 95.0, 98.0)] * 10
    day_specs = pre + [
        (101.0, 100.0, 100.5),  # day10: breakout
        (100.2, 98.0, 99.0),    # day11: immediate V-shaped dip back through the level
    ]
    results = _breakout_and_days(day_specs, lookback=5, min_separation_days=2, min_extension_pct=0.01)
    entry_day11, eligible_day11 = results[-1]

    assert entry_day11["retest_seen"] is False
    assert eligible_day11 is False


# --- failed breakout is sticky ---

def test_failed_breakout_is_sticky_until_fresh_breakout():
    pre = [(100.0, 95.0, 98.0)] * 10
    day_specs = pre + [
        (101.0, 100.0, 100.5),  # day10: breakout, level=100
        (91.0, 84.0, 85.0),     # day11: close 85 < 90 (100 * (1 - 0.10)) -> failed
        (96.0, 94.0, 95.0),     # day12: "recovers" above 90, but must STAY failed
    ]
    results = _breakout_and_days(day_specs, lookback=5, failed_breakout_depth=0.10)
    entry_day11, eligible_day11 = results[-2]
    entry_day12, eligible_day12 = results[-1]

    assert entry_day11["failed"] is True
    assert eligible_day11 is False
    # Sticky: still failed on day12 despite the close recovering above the
    # failed-breakout threshold -- does not un-fail on its own.
    assert entry_day12["failed"] is True
    assert eligible_day12 is False


def test_fresh_breakout_overwrites_a_failed_entry():
    prior_entry = {
        "breakout_date": "2023-01-01",
        "breakout_level": 50.0,
        "breakout_volume": 1_000_000.0,
        "retest_seen": False,
        "retest_low": None,
        "failed": True,
        "max_close_since_breakout": 50.0,
        "extension_confirmed": False,
        "eligible_for_entry": False,
    }
    highs = [100.0] * 25 + [110.0]
    lows = [95.0] * 26
    closes = [98.0] * 25 + [105.0]
    bars = _bars(highs, lows, closes)

    entry, eligible = update_watchlist_entry(prior_entry, bars, _cfg(lookback_days=20))

    assert entry["failed"] is False
    assert entry["breakout_level"] == 100.0


# --- age-out: a breakout too old is dropped entirely ---

def test_breakout_older_than_max_age_is_dropped():
    pre = [(100.0, 95.0, 98.0)] * 10
    day_specs = pre + [
        (101.0, 100.0, 100.5),  # day10: breakout
        (100.5, 100.0, 100.2),  # day11: days_since=1
        (100.5, 100.0, 100.2),  # day12: days_since=2
        (100.5, 100.0, 100.2),  # day13: days_since=3
        (100.5, 100.0, 100.2),  # day14: days_since=4 -> exceeds max_breakout_age_days=3
    ]
    results = _breakout_and_days(day_specs, lookback=5, max_breakout_age_days=3)
    entry_day14, eligible_day14 = results[-1]

    assert entry_day14 is None
    assert eligible_day14 is False


def test_insufficient_history_leaves_entry_unchanged():
    # lookback_days=5 requires n >= lookback+5=10 bars before evaluating
    # anything -- 9 bars must leave the entry untouched.
    highs = [100.0] * 9
    lows = [95.0] * 9
    closes = [98.0] * 9
    bars = _bars(highs, lows, closes)

    prior_entry = {"breakout_level": 42.0, "failed": False}
    entry, eligible = update_watchlist_entry(prior_entry, bars, _cfg(lookback_days=5))

    assert entry == prior_entry
    assert eligible is False


def test_no_breakout_and_nothing_tracked_returns_none():
    # Strictly declining highs -- today's high is never >= the prior
    # lookback-day high, so no breakout ever fires.
    n = 30
    highs = [100.0 - 0.1 * i for i in range(n)]
    lows = [h - 5 for h in highs]
    closes = [h - 2 for h in highs]
    bars = _bars(highs, lows, closes)

    entry, eligible = update_watchlist_entry(None, bars, _cfg(lookback_days=20))

    assert entry is None
    assert eligible is False
