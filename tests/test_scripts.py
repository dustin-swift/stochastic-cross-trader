import json
import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent

BASE_CFG = {
    "live": False,
    "account_number": "849995824",
    "screening": {
        "finviz_csv_path": "finviz_export.csv",  # overridden per-test to a tmp_path fixture
        "max_candidates_per_sector": 5,
    },
    "stochastic": {"k_period": 14, "k_smooth": 3, "d_period": 3, "oversold_threshold": 20, "overbought_threshold": 80},
    "atr": {"period": 14, "stop_multiplier": 1.5},
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


def test_check_universe_screen_end_to_end(tmp_path):
    # Fresh file written now -> mtime is "today", always passes the staleness
    # check regardless of what the real calendar date happens to be.
    csv_path = tmp_path / "finviz_export.csv"
    csv_path.write_text(
        "Ticker,Sector,Price\n"
        "A,Tech,10\n"
        "B,Tech,11\n"
        "C,Health,12\n"
        "D,Tech,13\n"
    )

    cfg = {**BASE_CFG, "screening": {"finviz_csv_path": str(csv_path), "max_candidates_per_sector": 2}}
    config_path = tmp_path / "strategy.yaml"
    with config_path.open("w") as f:
        yaml.safe_dump(cfg, f)

    result = _run(
        "check_universe_screen.py",
        None,
        ["--config", str(config_path), "--data-dir", str(tmp_path / "data")],
    )

    output = json.loads(result.stdout)
    symbols = {c["symbol"] for c in output}
    assert symbols == {"A", "B", "C"}  # 3rd Tech (D) trimmed by the sector cap

    candidates_file = tmp_path / "data" / "candidates.json"
    assert candidates_file.exists()
    assert {c["symbol"] for c in json.loads(candidates_file.read_text())} == symbols

    log_files = list((tmp_path / "data" / "logs").glob("*.jsonl"))
    assert len(log_files) == 1
    record = json.loads(log_files[0].read_text().splitlines()[0])
    assert record["event"] == "universe_screen"
    assert record["input_count"] == 4
    assert record["output_count"] == 3


def test_check_universe_screen_excludes_earnings_too_close(tmp_path):
    from datetime import date, timedelta

    csv_path = tmp_path / "finviz_export.csv"
    csv_path.write_text(
        "Ticker,Sector,Price\n"
        "A,Tech,10\n"
        "B,Tech,11\n"
        "C,Health,12\n"
    )

    cfg = {**BASE_CFG, "screening": {"finviz_csv_path": str(csv_path), "max_candidates_per_sector": 5}}
    config_path = tmp_path / "strategy.yaml"
    with config_path.open("w") as f:
        yaml.safe_dump(cfg, f)

    soon = (date.today() + timedelta(days=2)).isoformat()
    far = (date.today() + timedelta(days=60)).isoformat()
    earnings = {"A": [soon], "B": [far]}  # C intentionally omitted -> "unknown" exclusion

    result = _run(
        "check_universe_screen.py",
        earnings,
        ["--config", str(config_path), "--data-dir", str(tmp_path / "data"), "--earnings-input", "-"],
    )

    output = json.loads(result.stdout)
    assert {c["symbol"] for c in output} == {"B"}

    log_files = list((tmp_path / "data" / "logs").glob("*.jsonl"))
    events = [json.loads(line) for line in log_files[0].read_text().splitlines()]
    excl_event = next(e for e in events if e["event"] == "universe_screen_earnings_excluded")
    assert set(excl_event["symbols"]) == {"A", "C"}
    assert excl_event["reasons"] == {"A": "too_close", "C": "unknown"}


def test_check_universe_screen_no_earnings_filtering_when_flag_omitted(tmp_path):
    # Backward compatibility: omitting --earnings-input must behave exactly
    # like before the feature existed -- no filtering, no exclusion event.
    csv_path = tmp_path / "finviz_export.csv"
    csv_path.write_text("Ticker,Sector,Price\nA,Tech,10\n")

    cfg = {**BASE_CFG, "screening": {"finviz_csv_path": str(csv_path), "max_candidates_per_sector": 5}}
    config_path = tmp_path / "strategy.yaml"
    with config_path.open("w") as f:
        yaml.safe_dump(cfg, f)

    result = _run(
        "check_universe_screen.py",
        None,
        ["--config", str(config_path), "--data-dir", str(tmp_path / "data")],
    )

    output = json.loads(result.stdout)
    assert {c["symbol"] for c in output} == {"A"}

    log_files = list((tmp_path / "data" / "logs").glob("*.jsonl"))
    events = [json.loads(line)["event"] for line in log_files[0].read_text().splitlines()]
    assert "universe_screen_earnings_excluded" not in events


def test_check_universe_screen_blocked_on_stale_csv(tmp_path):
    import os
    from datetime import datetime, timedelta

    csv_path = tmp_path / "finviz_export.csv"
    csv_path.write_text("Ticker,Sector\nA,Tech\n")
    old = (datetime.now() - timedelta(days=30)).timestamp()
    os.utime(csv_path, (old, old))

    cfg = {**BASE_CFG, "screening": {"finviz_csv_path": str(csv_path), "max_candidates_per_sector": 5}}
    config_path = tmp_path / "strategy.yaml"
    with config_path.open("w") as f:
        yaml.safe_dump(cfg, f)

    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "check_universe_screen.py"),
         "--config", str(config_path), "--data-dir", str(tmp_path / "data")],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )

    assert result.returncode != 0
    assert "stale" in result.stderr.lower()
    assert not (tmp_path / "data" / "candidates.json").exists()

    log_files = list((tmp_path / "data" / "logs").glob("*.jsonl"))
    assert len(log_files) == 1
    record = json.loads(log_files[0].read_text().splitlines()[0])
    assert record["event"] == "universe_screen_blocked"


def test_check_universe_screen_alerts_on_zero_candidates(tmp_path):
    csv_path = tmp_path / "finviz_export.csv"
    csv_path.write_text("Ticker,Sector\n")  # header only, no rows

    cfg = {**BASE_CFG, "screening": {"finviz_csv_path": str(csv_path), "max_candidates_per_sector": 5}}
    config_path = tmp_path / "strategy.yaml"
    with config_path.open("w") as f:
        yaml.safe_dump(cfg, f)

    result = _run(
        "check_universe_screen.py",
        None,
        ["--config", str(config_path), "--data-dir", str(tmp_path / "data")],
    )

    assert json.loads(result.stdout) == []
    assert (tmp_path / "data" / "candidates.json").exists()

    log_files = list((tmp_path / "data" / "logs").glob("*.jsonl"))
    events = [json.loads(line)["event"] for line in log_files[0].read_text().splitlines()]
    assert "universe_screen_empty" in events
    assert "universe_screen" in events


def test_check_hourly_signals_end_to_end(tmp_path):
    cfg = {
        **BASE_CFG,
        "stochastic": {"k_period": 3, "k_smooth": 1, "d_period": 2, "oversold_threshold": 20, "overbought_threshold": 80},
    }
    config_path = tmp_path / "strategy.yaml"
    with config_path.open("w") as f:
        yaml.safe_dump(cfg, f)

    # high=50, low=0 fixed for every bar so raw %K = 2 * close, independent of
    # rolling window content -> lets us hand-pick exact k values (see plan notes).
    def bar(close):
        return {"high": 50, "low": 0, "close": close}

    # k sequence (from index 2): 16, 14, 24 -> entry: prior bar oversold (14<20),
    # k crosses above 20, k crosses above d in the same bar (see derivation in
    # the test suite for lib/signals.py).
    entry_closes = [10, 10, 8, 7, 12]
    # k sequence: 24, 28, 16 -> bearish crossover (k crosses below d).
    exit_closes = [10, 10, 12, 14, 8]

    payload = {
        "candidates": [
            {
                "symbol": "AAPL",
                "sector": "Technology",
                "atr14": 2.0,
                "bars": [bar(c) for c in entry_closes],
            }
        ],
        "open_positions": [
            {
                "symbol": "MSFT",
                "entry_price": 300.0,
                "qty": 0.33,
                "bars": [bar(c) for c in exit_closes],
            }
        ],
    }

    result = _run(
        "check_hourly_signals.py",
        payload,
        ["--config", str(config_path), "--data-dir", str(tmp_path / "data")],
    )

    output = json.loads(result.stdout)

    assert [e["symbol"] for e in output["entries"]] == ["AAPL"]
    entry = output["entries"][0]
    assert entry["k"] == 24
    assert entry["d"] == 19
    assert entry["last_close"] == 12
    assert entry["estimated_stop_price"] == 9.0  # 12 - 1.5*2.0
    assert entry["qty"] == 8  # round(100 / 12)

    assert [e["symbol"] for e in output["exits"]] == ["MSFT"]
    assert output["exits"][0]["stochastic_state"] == "NORMAL"

    assert [s["symbol"] for s in output["position_states"]] == ["MSFT"]
    assert output["position_states"][0]["stochastic_state"] == "NORMAL"

    log_files = list((tmp_path / "data" / "logs").glob("*.jsonl"))
    assert len(log_files) == 1
    events = [json.loads(line) for line in log_files[0].read_text().splitlines()]
    assert {(e["symbol"], e["kind"]) for e in events} == {("AAPL", "entry"), ("MSFT", "exit")}


def test_check_hourly_signals_skips_entry_over_price_cap(tmp_path):
    cfg = {
        **BASE_CFG,
        "stochastic": {"k_period": 3, "k_smooth": 1, "d_period": 2, "oversold_threshold": 20, "overbought_threshold": 80},
    }
    config_path = tmp_path / "strategy.yaml"
    with config_path.open("w") as f:
        yaml.safe_dump(cfg, f)

    # Same k-sequence-producing closes as the end-to-end test, scaled up so
    # the signal bar's close (last_close) lands above max_price_per_share (150).
    def bar(close):
        return {"high": close * 5, "low": 0, "close": close}

    entry_closes = [1000, 1000, 800, 700, 1200]  # last close 1200 > 150 cap

    payload = {"candidates": [{"symbol": "EXPENSIVE", "sector": "Technology", "atr14": 20.0, "bars": [bar(c) for c in entry_closes]}]}

    result = _run("check_hourly_signals.py", payload, ["--config", str(config_path), "--data-dir", str(tmp_path / "data")])
    output = json.loads(result.stdout)

    assert output["entries"] == []

    events = _events(tmp_path)
    skip_events = [e for e in events if e["event"] == "entry_skipped_price_cap"]
    assert len(skip_events) == 1
    assert skip_events[0]["symbol"] == "EXPENSIVE"
    assert skip_events[0]["last_close"] == 1200
    assert skip_events[0]["max_price_per_share"] == 150


def test_check_hourly_signals_skips_candidate_entry_when_earnings_too_close(tmp_path):
    from datetime import date, timedelta

    cfg = {
        **BASE_CFG,
        "stochastic": {"k_period": 3, "k_smooth": 1, "d_period": 2, "oversold_threshold": 20, "overbought_threshold": 80},
    }
    config_path = tmp_path / "strategy.yaml"
    with config_path.open("w") as f:
        yaml.safe_dump(cfg, f)

    def bar(close):
        return {"high": 50, "low": 0, "close": close}

    entry_closes = [10, 10, 8, 7, 12]  # would otherwise signal entry (see earlier derivation)
    soon = (date.today() + timedelta(days=2)).isoformat()  # inside the default 5-day exclusion window

    payload = {
        "candidates": [
            {
                "symbol": "AAPL",
                "sector": "Technology",
                "atr14": 2.0,
                "earnings_report_dates": [soon],
                "bars": [bar(c) for c in entry_closes],
            }
        ],
        "open_positions": [],
    }

    result = _run(
        "check_hourly_signals.py",
        payload,
        ["--config", str(config_path), "--data-dir", str(tmp_path / "data")],
    )

    output = json.loads(result.stdout)
    assert output["entries"] == []

    events = _events(tmp_path)
    entry_event = next(e for e in events if e["symbol"] == "AAPL" and e["kind"] == "entry")
    assert entry_event["skipped"] == "earnings_too_close"


def test_check_hourly_signals_force_exits_position_when_earnings_too_close(tmp_path):
    from datetime import date, timedelta

    cfg = {
        **BASE_CFG,
        "stochastic": {"k_period": 3, "k_smooth": 1, "d_period": 2, "oversold_threshold": 20, "overbought_threshold": 80},
    }
    config_path = tmp_path / "strategy.yaml"
    with config_path.open("w") as f:
        yaml.safe_dump(cfg, f)

    def bar(close):
        return {"high": 50, "low": 0, "close": close}

    # Bars that would NOT otherwise trigger any oscillator exit (flat/rising,
    # comfortably mid-range) -- proves the forced exit fires independent of
    # what the oscillator says, not just alongside a signal that would fire anyway.
    flat_closes = [20, 20, 22, 23, 25]
    tomorrow = (date.today() + timedelta(days=1)).isoformat()  # inside the default 1-day forced-exit window

    payload = {
        "candidates": [],
        "open_positions": [
            {
                "symbol": "MSFT",
                "entry_price": 20.0,
                "qty": 5.0,
                "stochastic_state": "NORMAL",
                "earnings_report_dates": [tomorrow],
                "bars": [bar(c) for c in flat_closes],
            }
        ],
    }

    result = _run(
        "check_hourly_signals.py",
        payload,
        ["--config", str(config_path), "--data-dir", str(tmp_path / "data")],
    )

    output = json.loads(result.stdout)
    assert [e["symbol"] for e in output["exits"]] == ["MSFT"]
    assert output["exits"][0]["exit_reason"] == "earnings_exit"


def test_check_hourly_signals_earnings_field_absent_does_not_force_exit(tmp_path):
    # No "earnings_report_dates" key at all on the position -- must fall
    # through to normal logic (no forced exit), not be treated as "unknown ->
    # exit" the way a missing entry does at the daily-screen level.
    cfg = {
        **BASE_CFG,
        "stochastic": {"k_period": 3, "k_smooth": 1, "d_period": 2, "oversold_threshold": 20, "overbought_threshold": 80},
    }
    config_path = tmp_path / "strategy.yaml"
    with config_path.open("w") as f:
        yaml.safe_dump(cfg, f)

    def bar(close):
        return {"high": 50, "low": 0, "close": close}

    payload = {
        "candidates": [],
        "open_positions": [
            {
                "symbol": "MSFT",
                "entry_price": 20.0,
                "qty": 5.0,
                "stochastic_state": "NORMAL",
                "bars": [bar(c) for c in [20, 20, 22, 23, 25]],
            }
        ],
    }

    result = _run(
        "check_hourly_signals.py",
        payload,
        ["--config", str(config_path), "--data-dir", str(tmp_path / "data")],
    )

    output = json.loads(result.stdout)
    assert output["exits"] == []
    assert output["position_states"][0]["symbol"] == "MSFT"


def test_check_hourly_signals_overbought_hold_suppresses_exit_and_persists_state(tmp_path):
    cfg = {
        **BASE_CFG,
        "stochastic": {
            "k_period": 3,
            "k_smooth": 1,
            "d_period": 2,
            "oversold_threshold": 20,
            "overbought_threshold": 80,
        },
    }
    config_path = tmp_path / "strategy.yaml"
    with config_path.open("w") as f:
        yaml.safe_dump(cfg, f)

    # high=50, low=0 fixed for every bar so raw %K = 2*close, independent of
    # rolling window content (same trick as the entry/exit test above).
    def bar(close):
        return {"high": 50, "low": 0, "close": close}

    # Cycle 1 bars, closes[2:] -> k = [80, 82, 90], d (2-period SMA of k) =
    # [NaN, 81, 86]. Last bar: k=90, d=86, both >= 80 -> transitions to
    # OVERBOUGHT_HOLD. Not an exit (both are well above 80, not below).
    cycle1_closes = [10, 10, 40, 41, 45]
    result1 = _run(
        "check_hourly_signals.py",
        {
            "candidates": [],
            "open_positions": [
                {"symbol": "NVDA", "entry_price": 100.0, "qty": 1.0, "bars": [bar(c) for c in cycle1_closes]}
            ],
        },
        ["--config", str(config_path), "--data-dir", str(tmp_path / "data")],
    )
    output1 = json.loads(result1.stdout)
    assert output1["position_states"] == [{"symbol": "NVDA", "k": 90.0, "d": 86.0, "stochastic_state": "OVERBOUGHT_HOLD"}]
    assert output1["exits"] == []

    # Cycle 2: a fresh bar window (as a real hourly cycle would fetch) whose
    # LAST bar, taken in isolation, is a textbook bearish crossover: k=[..,
    # 88, 78], d=[.., 84, 83] -> prev_k(88)>=prev_d(84) and cur_k(78)<cur_d(83).
    # Under STATE_NORMAL this would exit. Feeding in the OVERBOUGHT_HOLD state
    # persisted from cycle 1 must suppress it, since d=83 is still >= 80 (only
    # one line dipped, not both).
    cycle2_closes = [10, 10, 40, 44, 39]
    result2 = _run(
        "check_hourly_signals.py",
        {
            "candidates": [],
            "open_positions": [
                {
                    "symbol": "NVDA",
                    "entry_price": 100.0,
                    "qty": 1.0,
                    "stochastic_state": output1["position_states"][0]["stochastic_state"],
                    "bars": [bar(c) for c in cycle2_closes],
                }
            ],
        },
        ["--config", str(config_path), "--data-dir", str(tmp_path / "data")],
    )
    output2 = json.loads(result2.stdout)
    assert output2["position_states"] == [{"symbol": "NVDA", "k": 78.0, "d": 83.0, "stochastic_state": "OVERBOUGHT_HOLD"}]
    assert output2["exits"] == []  # suppressed: would have fired under STATE_NORMAL

    # Cycle 3: both lines now genuinely below 80 (k=70, d=74) -> the hold-exit fires.
    cycle3_closes = [10, 10, 40, 39, 35]
    result3 = _run(
        "check_hourly_signals.py",
        {
            "candidates": [],
            "open_positions": [
                {
                    "symbol": "NVDA",
                    "entry_price": 100.0,
                    "qty": 1.0,
                    "stochastic_state": output2["position_states"][0]["stochastic_state"],
                    "bars": [bar(c) for c in cycle3_closes],
                }
            ],
        },
        ["--config", str(config_path), "--data-dir", str(tmp_path / "data")],
    )
    output3 = json.loads(result3.stdout)
    assert output3["position_states"][0]["stochastic_state"] == "OVERBOUGHT_HOLD"  # sticky until exit
    assert [e["symbol"] for e in output3["exits"]] == ["NVDA"]


def test_check_hourly_signals_handles_string_typed_bar_values(tmp_path):
    # Robinhood's get_equity_historicals returns high/low/close as JSON
    # strings (e.g. "253.595000"), not numbers — regression test for a real
    # bug caught in a live dry run where this crashed lib.indicators.stochastic.
    cfg = {
        **BASE_CFG,
        "stochastic": {"k_period": 3, "k_smooth": 1, "d_period": 2, "oversold_threshold": 20, "overbought_threshold": 80},
    }
    config_path = tmp_path / "strategy.yaml"
    with config_path.open("w") as f:
        yaml.safe_dump(cfg, f)

    def bar(close):
        return {"high": "50.000000", "low": "0.000000", "close": f"{close}.000000"}

    entry_closes = [10, 10, 8, 7, 12]

    payload = {
        "candidates": [
            {"symbol": "AAPL", "sector": "Technology", "atr14": 2.0, "bars": [bar(c) for c in entry_closes]}
        ],
        "open_positions": [],
    }

    result = _run(
        "check_hourly_signals.py",
        payload,
        ["--config", str(config_path), "--data-dir", str(tmp_path / "data")],
    )

    output = json.loads(result.stdout)
    assert [e["symbol"] for e in output["entries"]] == ["AAPL"]
    assert output["entries"][0]["last_close"] == 12.0
    assert output["entries"][0]["estimated_stop_price"] == 9.0


def _write_cfg(tmp_path, overrides=None):
    cfg = {**BASE_CFG, **(overrides or {})}
    config_path = tmp_path / "strategy.yaml"
    with config_path.open("w") as f:
        yaml.safe_dump(cfg, f)
    return config_path


def _events(tmp_path):
    log_files = list((tmp_path / "data" / "logs").glob("*.jsonl"))
    assert len(log_files) == 1
    return [json.loads(line) for line in log_files[0].read_text().splitlines()]


def test_check_circuit_breaker_not_tripped(tmp_path):
    config_path = _write_cfg(tmp_path)
    payload = {"account_value": 1500, "realized_pnl_today": 5, "unrealized_pnl_today": 2}

    result = _run(
        "check_circuit_breaker.py", payload, ["--config", str(config_path), "--data-dir", str(tmp_path / "data")]
    )

    assert json.loads(result.stdout) == {"tripped": False}
    events = [e["event"] for e in _events(tmp_path)]
    assert events == ["circuit_breaker_check"]


def test_check_circuit_breaker_tripped_logs_and_alerts(tmp_path):
    config_path = _write_cfg(tmp_path)
    payload = {"account_value": 1500, "realized_pnl_today": -30, "unrealized_pnl_today": -20}

    result = _run(
        "check_circuit_breaker.py", payload, ["--config", str(config_path), "--data-dir", str(tmp_path / "data")]
    )

    assert json.loads(result.stdout) == {"tripped": True}
    events = [e["event"] for e in _events(tmp_path)]
    assert events == ["circuit_breaker_check", "circuit_breaker_triggered"]


def test_evaluate_order_fill_proceed_no_alert(tmp_path):
    config_path = _write_cfg(tmp_path)
    payload = {
        "symbol": "AAPL",
        "order_id": "order-1",
        "order_state": "filled",
        "filled_qty": 0.5,
        "requested_qty": 0.5,
        "elapsed_seconds": 3,
    }

    result = _run(
        "evaluate_order_fill.py", payload, ["--config", str(config_path), "--data-dir", str(tmp_path / "data")]
    )

    output = json.loads(result.stdout)
    assert output["decision"] == "proceed"
    assert output["symbol"] == "AAPL"
    events = [e["event"] for e in _events(tmp_path)]
    assert events == ["order_fill_check"]


def test_evaluate_order_fill_rejected_logs_and_alerts(tmp_path):
    config_path = _write_cfg(tmp_path)
    payload = {
        "symbol": "AAPL",
        "order_id": "order-1",
        "order_state": "rejected",
        "filled_qty": 0,
        "requested_qty": 0.5,
        "elapsed_seconds": 3,
    }

    result = _run(
        "evaluate_order_fill.py", payload, ["--config", str(config_path), "--data-dir", str(tmp_path / "data")]
    )

    output = json.loads(result.stdout)
    assert output["decision"] == "rejected"
    events = [e["event"] for e in _events(tmp_path)]
    assert events == ["order_fill_check", "entry_order_rejected_or_timeout"]


def test_evaluate_order_fill_timeout_uses_config_default(tmp_path):
    config_path = _write_cfg(tmp_path)  # poll_timeout_seconds: 30
    payload = {
        "symbol": "AAPL",
        "order_id": "order-1",
        "order_state": "new",
        "filled_qty": 0,
        "requested_qty": 0.5,
        "elapsed_seconds": 30,  # no timeout_seconds override -> uses config's 30
    }

    result = _run(
        "evaluate_order_fill.py", payload, ["--config", str(config_path), "--data-dir", str(tmp_path / "data")]
    )

    output = json.loads(result.stdout)
    assert output["decision"] == "timeout"
    assert output["needs_cancel"] is True


def test_record_stop_failure_always_logs_and_alerts(tmp_path):
    payload = {"symbol": "AAPL", "qty": 0.5, "fill_price": 210.5, "error": "insufficient buying power"}

    result = _run("record_stop_failure.py", payload, ["--data-dir", str(tmp_path / "data")])

    assert json.loads(result.stdout) == payload
    events = [e["event"] for e in _events(tmp_path)]
    assert events == ["stop_placement_failed"]


def test_record_trade_close_writes_history_and_logs(tmp_path):
    payload = {
        "symbol": "AAPL",
        "position": {
            "entry_price": 200.0,
            "qty": 0.5,
            "entry_time": "2026-07-29T14:30:00Z",
            "entry_order_id": "entry-1",
            "stop_price": 195.0,
            "stop_order_id": "stop-1",
        },
        "exit_price": 205.0,
        "exit_time": "2026-07-30T15:00:00Z",
        "exit_order_id": "stop-1",
        "exit_reason": "stop_out",
    }

    result = _run("record_trade_close.py", payload, ["--data-dir", str(tmp_path / "data")])

    output = json.loads(result.stdout)
    assert output["symbol"] == "AAPL"
    assert output["pnl_usd"] == 2.5
    assert output["exit_reason"] == "stop_out"

    with (tmp_path / "data" / "trade_history.json").open() as f:
        history = json.load(f)
    assert history == [output]

    events = [e["event"] for e in _events(tmp_path)]
    assert events == ["trade_closed"]


def test_build_dashboard_renders_html_from_repo_state(tmp_path):
    import yaml as _yaml
    from datetime import datetime, timezone

    data_dir = tmp_path / "data"
    (data_dir / "logs").mkdir(parents=True)

    (data_dir / "positions.json").write_text(json.dumps({
        "AAPL": {"entry_price": 200.0, "qty": 0.1, "entry_time": "2026-07-30T14:00:00Z",
                  "entry_order_id": "e1", "stop_order_id": "s1", "stop_price": 195.0, "stochastic_state": "NORMAL"},
    }))
    (data_dir / "trade_history.json").write_text(json.dumps([
        {"symbol": "MSFT", "qty": 0.2, "entry_price": 400.0, "exit_price": 410.0,
         "pnl_usd": 2.0, "pnl_pct": 2.5, "exit_reason": "signal_exit",
         "entry_time": "2026-07-28T14:00:00Z", "exit_time": "2026-07-29T14:00:00Z",
         "entry_order_id": "e2", "exit_order_id": "s2", "stop_price": 390.0, "closed_at": "2026-07-29T14:00:05Z"},
    ]))
    (data_dir / "candidates.json").write_text(json.dumps([
        {"symbol": "AAPL", "sector": "Technology", "Price": "200.00", "Change": "1.00%"},
    ]))

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log_path = data_dir / "logs" / f"{today}.jsonl"
    log_path.write_text(
        json.dumps({"timestamp": f"{today}T14:00:00+00:00", "event": "circuit_breaker_check",
                    "tripped": False, "account_value": 1500}) + "\n"
        + json.dumps({"timestamp": f"{today}T14:00:01+00:00", "event": "signal_check"}) + "\n"
    )

    config_path = tmp_path / "strategy.yaml"
    with config_path.open("w") as f:
        _yaml.safe_dump(BASE_CFG, f)

    payload = {
        "account": {"total_value": 1500.0, "cash": 1400.0, "buying_power": 1400.0,
                    "equity_value": 100.0, "realized_pnl_all_time": 2.0},
        "position_prices": {"AAPL": 205.0},
    }
    output_path = tmp_path / "dist.html"

    _run("build_dashboard.py", payload, [
        "--config", str(config_path),
        "--data-dir", str(data_dir),
        "--dashboard-dir", str(REPO_ROOT / "dashboard"),
        "--output", str(output_path),
    ])

    html = output_path.read_text()
    assert "Stochastic Pullback Trader" in html
    assert "@font-face" in html

    data_marker = 'type="application/json">'
    start = html.index(data_marker) + len(data_marker)
    end = html.index("</script>", start)
    embedded = json.loads(html[start:end])
    assert embedded["account"]["realized_pnl_all_time"] == 2.0
    assert embedded["positions"][0]["symbol"] == "AAPL"
    assert embedded["positions"][0]["current_price"] == 205.0
    assert embedded["trade_history"][0]["symbol"] == "MSFT"
    assert embedded["candidates"][0]["symbol"] == "AAPL"
    assert embedded["circuit_breaker"]["tripped"] is False
    assert embedded["activity_today"]["signal_check"] == 1
    assert any(r["name"] == "dashboard-refresh" for r in embedded["routines"])


def test_run_backtest_end_to_end(tmp_path):
    cfg = {
        **BASE_CFG,
        "stochastic": {"k_period": 3, "k_smooth": 1, "d_period": 2, "oversold_threshold": 20, "overbought_threshold": 80},
    }
    config_path = tmp_path / "strategy.yaml"
    with config_path.open("w") as f:
        yaml.safe_dump(cfg, f)

    def bar(t, close, high=1000, low=100):
        return {"begins_at": t, "high": high, "low": low, "close": close}

    times = [f"2026-07-20T{h:02d}:00:00Z" for h in range(6)]
    # Same fixed-high/low trick as tests/test_backtest.py: closes ->
    # entry at t4 (raw %K 16,14,24 -> crosses above 20 and above %D),
    # then a small ATR forces an immediate stop-loss at t5.
    closes = [150, 150, 244, 226, 316, 460]
    bars = [bar(t, c) for t, c in zip(times, closes)]

    payload = {
        "symbol_data": {
            "XXX": {"bars": bars, "atr_series": [{"begins_at": times[4], "value": 1.0}]},
        }
    }

    result = _run(
        "run_backtest.py",
        payload,
        ["--config", str(config_path), "--data-dir", str(tmp_path / "data")],
    )

    output = json.loads(result.stdout)
    assert len(output["trades"]) == 1
    trade = output["trades"][0]
    assert trade["symbol"] == "XXX"
    assert trade["exit_reason"] == "stop_loss"
    assert output["summary"]["trade_count"] == 1

    results_file = tmp_path / "data" / "backtest_results.json"
    assert results_file.exists()
    assert json.loads(results_file.read_text()) == output

    events = [e["event"] for e in _events(tmp_path)]
    assert events == ["backtest_run"]
