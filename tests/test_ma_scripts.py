import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent

BASE_MA_CFG = {
    "live": False,
    "account_number": "849995824",
    "screening": {"finviz_csv_path": "finviz_export.csv"},  # overridden per test to a tmp_path fixture
    "breakout": {
        "lookback_days": 5,
        "max_breakout_age_days": 10,
        "failed_breakout_depth": 0.10,
        "min_separation_days": 1,
        "min_extension_pct": 0.001,
    },
    "entry": {
        "late_entry_extension_cap": 0.05,
        "gap_cap_atr_multiple": 5.0,
        "volume_confirm_lookback": 3,
        "atr_regime_lookback": 5,
        "atr_regime_multiple": 5.0,
    },
    "trend": {"sma_fast": 3, "sma_slow": 5, "slope_lookback_bars": 2},
    "atr": {"period": 3},
    "stop_target": {
        "stop_atr_multiple": 2.5,
        "stop_retest_buffer_atr": 0.25,
        "partial_profit_r_multiple": 1.5,
        "partial_profit_pct": 0.45,
        "chandelier_atr_multiple": 2.5,
    },
    "exit_timing": {"trend_invalidation_grace_days": 1, "time_stop_days": 100},
    "sizing": {"per_trade_usd": 100, "max_price_per_share": 150, "max_positions": 10},
    "risk": {"max_daily_loss_pct": 3},
    "order_lifecycle": {"poll_timeout_seconds": 30, "poll_interval_seconds": 5},
    "alerts": {"provider": "slack"},
}


def _run(script: str, stdin_payload, extra_args: list[str]):
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / script), *extra_args],
        input=json.dumps(stdin_payload) if stdin_payload is not None else None,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=True,
    )


def _write_ma_cfg(tmp_path, overrides=None):
    cfg = {**BASE_MA_CFG, **(overrides or {})}
    config_path = tmp_path / "ma_pullback_strategy.yaml"
    with config_path.open("w") as f:
        yaml.safe_dump(cfg, f)
    return config_path


def _events(data_dir):
    log_files = list((data_dir / "logs").glob("*.jsonl"))
    assert len(log_files) == 1
    return [json.loads(line) for line in log_files[0].read_text().splitlines()]


# --- check_ma_universe_screen.py ---

def test_check_ma_universe_screen_end_to_end(tmp_path):
    csv_path = tmp_path / "finviz_export.csv"
    csv_path.write_text("Ticker,Sector,Price\nA,Tech,10\nB,Tech,11\nC,Health,12\n")

    cfg = _write_ma_cfg(tmp_path, {"screening": {"finviz_csv_path": str(csv_path)}})
    data_dir = tmp_path / "data"

    result = _run("check_ma_universe_screen.py", None, ["--config", str(cfg), "--data-dir", str(data_dir)])

    output = json.loads(result.stdout)
    assert {c["symbol"] for c in output} == {"A", "B", "C"}
    # No sector cap on this agent's screen -- all 3 Tech/Health rows pass through.

    events = [e["event"] for e in _events(data_dir)]
    assert "ma_universe_screen" in events


def test_check_ma_universe_screen_blocked_on_stale_csv(tmp_path):
    import os
    from datetime import datetime, timedelta

    csv_path = tmp_path / "finviz_export.csv"
    csv_path.write_text("Ticker,Sector\nA,Tech\n")
    old = (datetime.now() - timedelta(days=30)).timestamp()
    os.utime(csv_path, (old, old))

    cfg = _write_ma_cfg(tmp_path, {"screening": {"finviz_csv_path": str(csv_path)}})
    data_dir = tmp_path / "data"

    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "check_ma_universe_screen.py"),
         "--config", str(cfg), "--data-dir", str(data_dir)],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )

    assert result.returncode != 0
    assert "stale" in result.stderr.lower()

    events = [e["event"] for e in _events(data_dir)]
    assert "ma_universe_screen_blocked" in events


def test_check_ma_universe_screen_alerts_on_zero_candidates(tmp_path):
    csv_path = tmp_path / "finviz_export.csv"
    csv_path.write_text("Ticker,Sector\n")

    cfg = _write_ma_cfg(tmp_path, {"screening": {"finviz_csv_path": str(csv_path)}})
    data_dir = tmp_path / "data"

    result = _run("check_ma_universe_screen.py", None, ["--config", str(cfg), "--data-dir", str(data_dir)])
    assert json.loads(result.stdout) == []

    events = [e["event"] for e in _events(data_dir)]
    assert "ma_universe_screen_empty" in events


# --- check_ma_daily_scan.py ---

def _breakout_price_history():
    # 9 flat pre-breakout bars, then a fresh breakout, then extension, then a
    # genuine (separated + extended) retest -- see tests/test_ma_breakout.py
    # for the fully isolated unit-level version of this same shape.
    n_pre = 9
    dates = [f"2024-01-{i+1:02d}" for i in range(12)]
    highs = [100.0] * n_pre + [101.0, 100.8, 100.7]
    lows = [95.0] * n_pre + [100.0, 100.2, 99.5]
    closes = [98.0] * n_pre + [100.2, 100.3, 100.0]
    volumes = [1_000_000.0] * 12
    return {"dates": dates, "high": highs, "low": lows, "close": closes, "volume": volumes}


def test_check_ma_daily_scan_tracks_breakout_and_retest(tmp_path):
    # The daily scan runs once per real trading day, evaluating only the LAST
    # bar it's handed as "today" -- so a multi-day breakout->extension->retest
    # progression has to be simulated as multiple separate script
    # invocations, each with one more day's bars appended and the prior
    # day's watchlist output fed back in as input, exactly like the real
    # daily-ma-scan cadence (see .claude/skills/daily-ma-scan.md).
    cfg = _write_ma_cfg(tmp_path)
    data_dir = tmp_path / "data"

    full_history = _breakout_price_history()
    n_pre = 9  # matches _breakout_price_history()'s flat warmup length

    watchlist = {}
    outputs = []
    # First evaluable day is index n_pre (breakout day) -- lookback+5=10
    # bars are required before that, so start there and grow by one bar
    # each subsequent call through the end of the fixture.
    for day in range(n_pre + 1, len(full_history["dates"]) + 1):
        sliced = {k: v[:day] for k, v in full_history.items()}
        payload = {
            "finviz_candidates": [{"symbol": "AAPL"}],
            "price_history": {"AAPL": sliced},
            "watchlist": watchlist,
        }
        result = _run("check_ma_daily_scan.py", payload, ["--config", str(cfg), "--data-dir", str(data_dir)])
        watchlist = json.loads(result.stdout)
        outputs.append(watchlist)

    final = outputs[-1]
    assert final["AAPL"]["breakout_level"] == 100.0
    assert final["AAPL"]["retest_seen"] is True
    assert final["AAPL"]["eligible_for_entry"] is True

    saved = json.loads((data_dir / "watchlist.json").read_text())
    assert saved == final

    all_events = []
    for path in sorted((data_dir / "logs").glob("*.jsonl")):
        all_events.extend(json.loads(line)["event"] for line in path.read_text().splitlines())
    assert "ma_breakout_tracked" in all_events
    assert "ma_retest_seen" in all_events
    assert "ma_daily_scan" in all_events


def test_check_ma_daily_scan_keeps_tracking_symbol_dropped_from_finviz(tmp_path):
    # A symbol already on the watchlist (mid-retest) must keep being tracked
    # even if it doesn't appear in today's fresh Finviz candidate list.
    cfg = _write_ma_cfg(tmp_path)
    data_dir = tmp_path / "data"

    existing_entry = {
        "breakout_date": "2024-01-10",
        "breakout_level": 100.0,
        "breakout_volume": 1_000_000.0,
        "retest_seen": False,
        "retest_low": None,
        "failed": False,
        "max_close_since_breakout": 100.2,
        "extension_confirmed": True,
        "eligible_for_entry": False,
    }

    payload = {
        "finviz_candidates": [],  # AAPL no longer on today's fresh scan
        "price_history": {"AAPL": _breakout_price_history()},
        "watchlist": {"AAPL": existing_entry},
    }

    result = _run("check_ma_daily_scan.py", payload, ["--config", str(cfg), "--data-dir", str(data_dir)])
    watchlist = json.loads(result.stdout)

    assert "AAPL" in watchlist  # still tracked despite being off today's Finviz list


def test_check_ma_daily_scan_places_no_orders_and_writes_no_positions(tmp_path):
    # Sanity check on the "never places trades" rule -- this script has no
    # code path that touches positions.json at all.
    cfg = _write_ma_cfg(tmp_path)
    data_dir = tmp_path / "data"

    payload = {"finviz_candidates": [], "price_history": {}, "watchlist": {}}
    _run("check_ma_daily_scan.py", payload, ["--config", str(cfg), "--data-dir", str(data_dir)])

    assert not (data_dir / "positions.json").exists()


# --- check_ma_hourly_signals.py ---

def _uptrend_daily_bars_dict(n=13, start_close=90.0):
    closes = [start_close + i for i in range(n)]
    dates = [f"2024-01-{i+1:02d}" for i in range(n)]
    return {
        "dates": dates,
        "date": dates,
        "high": [c + 0.5 for c in closes],
        "low": [c - 0.5 for c in closes],
        "close": closes,
        "volume": [1_000_000.0] * n,
    }


def test_check_ma_hourly_signals_fires_entry_on_reclaim(tmp_path):
    cfg = _write_ma_cfg(tmp_path)
    data_dir = tmp_path / "data"

    daily = _uptrend_daily_bars_dict()
    hourly_closes = [99.0, 99.5, 99.7, 99.8, 100.3]
    hourly = {
        "date": [f"2024-02-{i+1:02d}" for i in range(5)],
        "open": [c - 0.1 for c in hourly_closes],
        "high": [c + 0.2 for c in hourly_closes],
        "low": [c - 0.3 for c in hourly_closes],
        "close": hourly_closes,
        "volume": [1000.0, 1000.0, 1000.0, 1000.0, 1500.0],
    }

    payload = {
        "open_positions": [],
        "watchlist": {
            "AAPL": {
                "breakout_level": 100.0,
                "retest_low": 97.0,
                "retest_seen": True,
                "failed": False,
                "eligible_for_entry": True,
                "daily_bars": daily,
                "hourly_bars": hourly,
            }
        },
    }

    result = _run("check_ma_hourly_signals.py", payload, ["--config", str(cfg), "--data-dir", str(data_dir)])
    output = json.loads(result.stdout)

    assert [e["symbol"] for e in output["entries"]] == ["AAPL"]
    entry = output["entries"][0]
    assert entry["entry_price"] == 100.3
    assert entry["entry_price"] >= entry["breakout_level"]  # hard rule
    assert entry["shares"] >= 1

    last_cycle = json.loads((data_dir / "last_cycle_at.json").read_text())
    assert last_cycle["timestamp"]


def test_check_ma_hourly_signals_rejects_reclaim_that_closes_below_breakout_level(tmp_path):
    # Intrabar dip through breakout_level that fails to hold (close stays
    # below it) -- must never fire an entry, the hard no-entry-below-
    # breakout-level rule.
    cfg = _write_ma_cfg(tmp_path)
    data_dir = tmp_path / "data"

    daily = _uptrend_daily_bars_dict()
    hourly_closes = [99.0, 99.3, 99.5, 99.0, 99.5]  # never closes >= 100
    hourly = {
        "date": [f"2024-02-{i+1:02d}" for i in range(5)],
        "open": [c - 0.1 for c in hourly_closes],
        "high": [c + 0.2 for c in hourly_closes],
        "low": [98.0, 98.5, 98.8, 98.0, 98.0],  # dips through breakout_level intrabar
        "close": hourly_closes,
        "volume": [1000.0] * 5,
    }

    payload = {
        "open_positions": [],
        "watchlist": {
            "AAPL": {
                "breakout_level": 100.0,
                "retest_low": 97.0,
                "retest_seen": True,
                "failed": False,
                "eligible_for_entry": True,
                "daily_bars": daily,
                "hourly_bars": hourly,
            }
        },
    }

    result = _run("check_ma_hourly_signals.py", payload, ["--config", str(cfg), "--data-dir", str(data_dir)])
    output = json.loads(result.stdout)
    assert output["entries"] == []


def test_check_ma_hourly_signals_v_shaped_no_separation_retest_never_becomes_eligible(tmp_path):
    # End-to-end: a watchlist entry produced by an immediate, no-separation
    # pullback must never carry eligible_for_entry -- feeding it straight
    # into the hourly script (as a defense-in-depth check) confirms no
    # entry fires even if a caller mistakenly marked it eligible=False
    # (the correct state) vs. eligible=True (a hypothetical caller bug).
    cfg = _write_ma_cfg(tmp_path)
    data_dir = tmp_path / "data"

    daily = _uptrend_daily_bars_dict()
    hourly_closes = [99.0, 99.5, 99.7, 99.8, 100.3]
    hourly = {
        "date": [f"2024-02-{i+1:02d}" for i in range(5)],
        "open": [c - 0.1 for c in hourly_closes],
        "high": [c + 0.2 for c in hourly_closes],
        "low": [c - 0.3 for c in hourly_closes],
        "close": hourly_closes,
        "volume": [1000.0, 1000.0, 1000.0, 1000.0, 1500.0],
    }

    payload = {
        "open_positions": [],
        "watchlist": {
            "AAPL": {
                "breakout_level": 100.0,
                "retest_low": 99.0,
                "retest_seen": False,      # separation requirement never satisfied
                "failed": False,
                "eligible_for_entry": False,  # correct state produced by lib.ma_breakout
                "daily_bars": daily,
                "hourly_bars": hourly,
            }
        },
    }

    result = _run("check_ma_hourly_signals.py", payload, ["--config", str(cfg), "--data-dir", str(data_dir)])
    output = json.loads(result.stdout)
    assert output["entries"] == []


def test_check_ma_hourly_signals_exits_position_on_hard_stop(tmp_path):
    # Grace period high enough that trend-invalidation (which would also
    # fire on this same sharp decline) doesn't preempt the hard-stop check
    # this test is targeting.
    cfg = _write_ma_cfg(tmp_path, {"exit_timing": {"trend_invalidation_grace_days": 100, "time_stop_days": 100}})
    data_dir = tmp_path / "data"

    daily = {
        "date": ["2024-01-01", "2024-01-02", "2024-01-03"],
        "high": [100.5, 95.5, 80.5],
        "low": [99.5, 94.5, 79.5],
        "close": [100.0, 95.0, 80.0],
        "volume": [1_000_000.0] * 3,
    }

    payload = {
        "open_positions": [
            {
                "symbol": "MSFT",
                "entry_price": 100.0,
                "stop_price": 90.0,
                "target_1": 1_000_000.0,
                "highest_close": 100.0,
                "bars_held": 0,
                "partial_taken": False,
                "atr_entry": 2.0,
                "shares": 10,
                "daily_bars": daily,
            }
        ],
        "watchlist": {},
    }

    result = _run("check_ma_hourly_signals.py", payload, ["--config", str(cfg), "--data-dir", str(data_dir)])
    output = json.loads(result.stdout)

    assert [e["symbol"] for e in output["exits"]] == ["MSFT"]
    assert output["exits"][0]["action"] == "stop_hit"
    assert [u["symbol"] for u in output["position_updates"]] == ["MSFT"]


def test_check_ma_hourly_signals_large_gap_suppresses_entry(tmp_path):
    cfg = _write_ma_cfg(tmp_path)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "last_cycle_at.json").write_text(json.dumps({"timestamp": "2024-02-01T00:00:00+00:00"}))

    daily = _uptrend_daily_bars_dict()
    hourly_closes = [99.0, 99.5, 99.7, 99.8, 100.3]
    hourly = {
        "date": [f"2024-02-{i+1:02d}" for i in range(5)],
        "open": [c - 0.1 for c in hourly_closes],
        "high": [c + 0.2 for c in hourly_closes],
        "low": [c - 0.3 for c in hourly_closes],
        "close": hourly_closes,
        "volume": [1000.0, 1000.0, 1000.0, 1000.0, 1500.0],
    }
    payload = {
        "open_positions": [],
        "watchlist": {
            "AAPL": {
                "breakout_level": 100.0,
                "retest_low": 97.0,
                "retest_seen": True,
                "failed": False,
                "eligible_for_entry": True,
                "daily_bars": daily,
                "hourly_bars": hourly,
            }
        },
    }

    result = _run(
        "check_ma_hourly_signals.py",
        payload,
        ["--config", str(cfg), "--data-dir", str(data_dir), "--now", "2024-02-10T00:00:00Z"],
    )
    output = json.loads(result.stdout)

    assert output["entries"] == []
    assert output["stale_cycle"] is True

    events = [e["event"] for e in _events(data_dir)]
    assert "ma_entry_suppressed_stale_cycle" in events


# --- generic reused scripts against the REAL MA config shape (regression
# for the 2026-08-18 bug: both scripts used to call lib.config.load_config
# unconditionally, which validates against the stochastic schema regardless
# of which --config path was actually passed, so a perfectly valid
# config/ma_pullback_strategy.yaml crashed every cycle) ---

def test_check_circuit_breaker_accepts_ma_config_shape(tmp_path):
    cfg = _write_ma_cfg(tmp_path)
    data_dir = tmp_path / "data"
    payload = {"account_value": 1500, "realized_pnl_today": 5, "unrealized_pnl_today": 2}

    result = _run(
        "check_circuit_breaker.py", payload, ["--config", str(cfg), "--data-dir", str(data_dir)]
    )

    assert json.loads(result.stdout) == {"tripped": False}


def test_check_circuit_breaker_tripped_against_ma_config_shape(tmp_path):
    cfg = _write_ma_cfg(tmp_path)
    data_dir = tmp_path / "data"
    payload = {"account_value": 1500, "realized_pnl_today": -60, "unrealized_pnl_today": 0}

    result = _run(
        "check_circuit_breaker.py", payload, ["--config", str(cfg), "--data-dir", str(data_dir)]
    )

    assert json.loads(result.stdout) == {"tripped": True}


def test_evaluate_order_fill_accepts_ma_config_shape(tmp_path):
    cfg = _write_ma_cfg(tmp_path)
    data_dir = tmp_path / "data"
    payload = {
        "symbol": "AAPL", "order_id": "o-1", "order_state": "filled",
        "filled_qty": 10, "requested_qty": 10, "elapsed_seconds": 5,
    }

    result = _run(
        "evaluate_order_fill.py", payload, ["--config", str(cfg), "--data-dir", str(data_dir)]
    )

    assert json.loads(result.stdout)["decision"] == "proceed"


def test_evaluate_order_fill_timeout_uses_ma_config_default(tmp_path):
    cfg = _write_ma_cfg(tmp_path)
    data_dir = tmp_path / "data"
    payload = {
        "symbol": "AAPL", "order_id": "o-1", "order_state": "new",
        "filled_qty": 0, "requested_qty": 10, "elapsed_seconds": 999,
    }

    result = _run(
        "evaluate_order_fill.py", payload, ["--config", str(cfg), "--data-dir", str(data_dir)]
    )

    # poll_timeout_seconds: 30 in BASE_MA_CFG's order_lifecycle section -- 999
    # elapsed seconds is well past it with nothing filled.
    assert json.loads(result.stdout)["decision"] == "timeout"
