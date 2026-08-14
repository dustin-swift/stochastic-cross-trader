import json
import subprocess
import sys
from pathlib import Path

import pytest
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


def test_check_universe_screen_resets_pending_entries(tmp_path):
    # Pending dual-cross entry state (spec §3, 2026-08-04 revision) is
    # intraday-only -- a stale %K-crossed-but-%D-hasn't setup from yesterday
    # must not survive into today's first hourly check.
    csv_path = tmp_path / "finviz_export.csv"
    csv_path.write_text("Ticker,Sector,Price\nA,Tech,10\n")

    cfg = {**BASE_CFG, "screening": {"finviz_csv_path": str(csv_path), "max_candidates_per_sector": 5}}
    config_path = tmp_path / "strategy.yaml"
    with config_path.open("w") as f:
        yaml.safe_dump(cfg, f)

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "pending_entries.json").write_text(json.dumps({"XYZ": {"k_at_cross": 22.5}}))

    _run("check_universe_screen.py", None, ["--config", str(config_path), "--data-dir", str(data_dir)])

    assert json.loads((data_dir / "pending_entries.json").read_text()) == {}


def test_check_universe_screen_parses_atr_from_finviz_column(tmp_path):
    csv_path = tmp_path / "finviz_export.csv"
    csv_path.write_text(
        "Ticker,Sector,Price,Average True Range\n"
        "A,Tech,10,1.39\n"
        "B,Tech,11,\n"  # blank cell -> None, not a crash
        "C,Health,12,0.42\n"
    )

    cfg = {**BASE_CFG, "screening": {"finviz_csv_path": str(csv_path), "max_candidates_per_sector": 5}}
    config_path = tmp_path / "strategy.yaml"
    with config_path.open("w") as f:
        yaml.safe_dump(cfg, f)

    result = _run(
        "check_universe_screen.py",
        None,
        ["--config", str(config_path), "--data-dir", str(tmp_path / "data")],
    )

    output = {c["symbol"]: c for c in json.loads(result.stdout)}
    assert output["A"]["atr14"] == 1.39
    assert output["B"]["atr14"] is None
    assert output["C"]["atr14"] == 0.42


def test_check_universe_screen_atr14_none_when_column_absent(tmp_path):
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
    assert output[0]["atr14"] is None


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

    # A reports tomorrow BMO -> exit_date is today (bare string = conservative
    # BMO default) -> today is the entry-blocked day. B is far out -> kept.
    soon = (date.today() + timedelta(days=1)).isoformat()
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

    # k sequence (from index 2): 16, 14, 30 -> dual-cross entry (spec
    # §3, 2026-08-04 revision, see lib.signals.advance_pending_entry): the
    # final bar's k crosses above 20 from below (14 -> 30) AND its d (2-period
    # SMA of k, 15 -> 22) *also* clears 20 on the same bar, so this fires in a
    # single one-shot call with no persisted `pending` state needed.
    entry_closes = [10, 10, 8, 7, 15]
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
    assert entry["k"] == 30
    assert entry["d"] == 22
    assert entry["prev_k"] == 14  # post-trade review data (2026-08-05): the bar before the trigger
    assert entry["prev_d"] == 15
    assert entry["last_close"] == 15
    assert entry["estimated_stop_price"] == 12.0  # 15 - 1.5*2.0
    assert entry["qty"] == 7  # round(100 / 15)

    assert [e["symbol"] for e in output["exits"]] == ["MSFT"]
    exit_ = output["exits"][0]
    assert exit_["stochastic_state"] == "NORMAL"
    assert exit_["k"] == 16
    assert exit_["d"] == 22
    assert exit_["prev_k"] == 28
    assert exit_["prev_d"] == 26

    assert [s["symbol"] for s in output["position_states"]] == ["MSFT"]
    assert output["position_states"][0]["stochastic_state"] == "NORMAL"

    log_files = list((tmp_path / "data" / "logs").glob("*.jsonl"))
    assert len(log_files) == 1
    events = [json.loads(line) for line in log_files[0].read_text().splitlines()]
    assert {(e["symbol"], e["kind"]) for e in events} == {("AAPL", "entry"), ("MSFT", "exit")}


def _entry_only_payload():
    def bar(close):
        return {"high": 50, "low": 0, "close": close}

    entry_closes = [10, 10, 8, 7, 15]  # same dual-cross fixture as the end-to-end test
    return {
        "candidates": [{"symbol": "AAPL", "sector": "Technology", "atr14": 2.0, "bars": [bar(c) for c in entry_closes]}],
        "open_positions": [],
    }


def _stoch_cfg():
    return {"k_period": 3, "k_smooth": 1, "d_period": 2, "oversold_threshold": 20, "overbought_threshold": 80}


def test_check_hourly_signals_first_run_no_last_cycle_at_fires_normally(tmp_path):
    # No data/last_cycle_at.json yet (a genuinely fresh setup, or the file
    # predates this feature) -- the missed-cycle guard must not suppress
    # anything when there's no prior cycle to compare against.
    cfg = {**BASE_CFG, "stochastic": _stoch_cfg()}
    config_path = tmp_path / "strategy.yaml"
    with config_path.open("w") as f:
        yaml.safe_dump(cfg, f)

    result = _run(
        "check_hourly_signals.py",
        _entry_only_payload(),
        ["--config", str(config_path), "--data-dir", str(tmp_path / "data"), "--now", "2026-08-04T15:47:00Z"],
    )
    output = json.loads(result.stdout)

    assert [e["symbol"] for e in output["entries"]] == ["AAPL"]
    assert output["stale_cycle"] is False
    assert output["cycle_gap_minutes"] is None

    # And it must have recorded this run's completion for next time.
    last_cycle = json.loads((tmp_path / "data" / "last_cycle_at.json").read_text())
    assert last_cycle["timestamp"] == "2026-08-04T15:47:00+00:00"


def test_check_hourly_signals_small_gap_fires_normally(tmp_path):
    cfg = {**BASE_CFG, "stochastic": _stoch_cfg()}
    config_path = tmp_path / "strategy.yaml"
    with config_path.open("w") as f:
        yaml.safe_dump(cfg, f)

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "last_cycle_at.json").write_text(json.dumps({"timestamp": "2026-08-04T14:50:00+00:00"}))  # 57 min ago

    result = _run(
        "check_hourly_signals.py",
        _entry_only_payload(),
        ["--config", str(config_path), "--data-dir", str(data_dir), "--now", "2026-08-04T15:47:00Z"],
    )
    output = json.loads(result.stdout)

    assert [e["symbol"] for e in output["entries"]] == ["AAPL"]
    assert output["stale_cycle"] is False
    assert output["cycle_gap_minutes"] == pytest.approx(57.0)


def test_check_hourly_signals_large_gap_suppresses_entry(tmp_path):
    # This is the exact scenario confirmed live on DNTH/ILF/PCAR (2026-08-04):
    # two scheduled cycles silently did nothing (an MCP connector naming
    # mismatch), so the next successful run's two-bar crossing check spanned
    # several real hours instead of one -- must suppress the entry rather
    # than fire on a stale comparison, but still record k/d/pending normally.
    cfg = {**BASE_CFG, "stochastic": _stoch_cfg()}
    config_path = tmp_path / "strategy.yaml"
    with config_path.open("w") as f:
        yaml.safe_dump(cfg, f)

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "last_cycle_at.json").write_text(json.dumps({"timestamp": "2026-08-03T23:07:00+00:00"}))  # ~16.7 hours ago

    result = _run(
        "check_hourly_signals.py",
        _entry_only_payload(),
        ["--config", str(config_path), "--data-dir", str(data_dir), "--now", "2026-08-04T15:47:00Z"],
    )
    output = json.loads(result.stdout)

    assert output["entries"] == []
    assert output["stale_cycle"] is True
    assert output["cycle_gap_minutes"] == pytest.approx(16.67 * 60, abs=1)

    # Pending state still advances normally -- only the entry itself is held back.
    assert output["candidate_states"] == [{"symbol": "AAPL", "k": 30.0, "d": 22.0, "pending": None}]

    log_files = list((data_dir / "logs").glob("*.jsonl"))
    events = [json.loads(line) for line in log_files[0].read_text().splitlines()]
    suppressed = [e for e in events if e["event"] == "entry_suppressed_stale_cycle"]
    assert len(suppressed) == 1
    assert suppressed[0]["symbol"] == "AAPL"
    assert suppressed[0]["k"] == 30.0
    assert suppressed[0]["d"] == 22.0
    assert suppressed[0]["max_cycle_gap_minutes"] == 90

    # And the run's own completion time still gets recorded, so the next
    # cycle (assuming normal cadence resumes) starts measuring fresh.
    last_cycle = json.loads((data_dir / "last_cycle_at.json").read_text())
    assert last_cycle["timestamp"] == "2026-08-04T15:47:00+00:00"


def test_check_hourly_signals_custom_max_cycle_gap_minutes(tmp_path):
    # A gap that's well under the default 90-minute threshold still
    # suppresses when the config lowers the threshold below it.
    cfg = {**BASE_CFG, "stochastic": {**_stoch_cfg(), "max_cycle_gap_minutes": 5}}
    config_path = tmp_path / "strategy.yaml"
    with config_path.open("w") as f:
        yaml.safe_dump(cfg, f)

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "last_cycle_at.json").write_text(json.dumps({"timestamp": "2026-08-04T15:37:00+00:00"}))  # 10 min ago

    result = _run(
        "check_hourly_signals.py",
        _entry_only_payload(),
        ["--config", str(config_path), "--data-dir", str(data_dir), "--now", "2026-08-04T15:47:00Z"],
    )
    output = json.loads(result.stdout)

    assert output["entries"] == []
    assert output["stale_cycle"] is True


def test_check_hourly_signals_last_cycle_at_override_takes_precedence(tmp_path):
    # Sell-first cycle ordering (2026-08-04): the exits call (candidates: [])
    # always runs first and overwrites data/last_cycle_at.json with THIS
    # cycle's own timestamp. A subsequent entries call in the same cycle must
    # use --last-cycle-at to measure the gap against the TRUE prior cycle,
    # not the exits call's just-written value -- otherwise the guard would
    # see a near-zero gap every time and never suppress anything.
    cfg = {**BASE_CFG, "stochastic": _stoch_cfg()}
    config_path = tmp_path / "strategy.yaml"
    with config_path.open("w") as f:
        yaml.safe_dump(cfg, f)

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    # Simulates the exits call having already run moments ago and overwritten
    # the file with (near-)this cycle's own timestamp.
    (data_dir / "last_cycle_at.json").write_text(json.dumps({"timestamp": "2026-08-04T15:46:55+00:00"}))

    result = _run(
        "check_hourly_signals.py",
        _entry_only_payload(),
        [
            "--config", str(config_path),
            "--data-dir", str(data_dir),
            "--now", "2026-08-04T15:47:00Z",
            "--last-cycle-at", "2026-08-03T23:07:00Z",  # the TRUE prior cycle, ~16.7 hours ago
        ],
    )
    output = json.loads(result.stdout)

    assert output["entries"] == []
    assert output["stale_cycle"] is True
    assert output["cycle_gap_minutes"] == pytest.approx(16.67 * 60, abs=1)

    # Still writes the new value at the end regardless of the override source.
    last_cycle = json.loads((data_dir / "last_cycle_at.json").read_text())
    assert last_cycle["timestamp"] == "2026-08-04T15:47:00+00:00"


def test_check_hourly_signals_skips_entry_over_price_cap(tmp_path):
    cfg = {
        **BASE_CFG,
        "stochastic": {"k_period": 3, "k_smooth": 1, "d_period": 2, "oversold_threshold": 20, "overbought_threshold": 80},
    }
    config_path = tmp_path / "strategy.yaml"
    with config_path.open("w") as f:
        yaml.safe_dump(cfg, f)

    # Same k-sequence-producing closes as the end-to-end test, scaled up
    # (fixed high=5000/low=0, same ratio as the fixed high=50/low=0 used
    # there) so the dual-cross still fires on the final bar but the signal
    # bar's close (last_close) lands above max_price_per_share (150).
    def bar(close):
        return {"high": 5000, "low": 0, "close": close}

    entry_closes = [1000, 1000, 800, 700, 1500]  # last close 1500 > 150 cap

    payload = {"candidates": [{"symbol": "EXPENSIVE", "sector": "Technology", "atr14": 20.0, "bars": [bar(c) for c in entry_closes]}]}

    result = _run("check_hourly_signals.py", payload, ["--config", str(config_path), "--data-dir", str(tmp_path / "data")])
    output = json.loads(result.stdout)

    assert output["entries"] == []

    events = _events(tmp_path)
    skip_events = [e for e in events if e["event"] == "entry_skipped_price_cap"]
    assert len(skip_events) == 1
    assert skip_events[0]["symbol"] == "EXPENSIVE"
    assert skip_events[0]["last_close"] == 1500
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

    entry_closes = [10, 10, 8, 7, 15]  # would otherwise signal entry (see earlier derivation)
    # Reports tomorrow, BMO (bare string = conservative default) -> exit_date
    # is today -> today is exactly the entry-blocked day.
    soon = (date.today() + timedelta(days=1)).isoformat()

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
    # Reports tomorrow, BMO (bare string = conservative default) -> exit_date
    # is today -> forced exit fires today, but only once near_close (see
    # test below for the "not yet near close" case) -- pin --now to today at
    # a late UTC hour so this test is deterministic regardless of when it
    # actually runs, matching the real default forced_exit_utc_hour=19.
    today = date.today()
    tomorrow = (today + timedelta(days=1)).isoformat()
    now_near_close = f"{today.isoformat()}T20:00:00Z"

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
        ["--config", str(config_path), "--data-dir", str(tmp_path / "data"), "--now", now_near_close],
    )

    output = json.loads(result.stdout)
    assert [e["symbol"] for e in output["exits"]] == ["MSFT"]
    assert output["exits"][0]["exit_reason"] == "earnings_exit"


def test_check_hourly_signals_does_not_force_exit_before_near_close(tmp_path):
    # This is the exact bug confirmed live on BALL (2026-08-03): the forced
    # exit fired on the FIRST regular-session check of exit_date instead of
    # the last, forfeiting most of that day's run-up. Same setup as the test
    # above, but --now pinned to early in the session -- must NOT force-exit.
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

    flat_closes = [20, 20, 22, 23, 25]
    today = date.today()
    tomorrow = (today + timedelta(days=1)).isoformat()
    now_first_check_of_day = f"{today.isoformat()}T13:45:00Z"  # 9:45am ET, first hourly cycle

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
        ["--config", str(config_path), "--data-dir", str(tmp_path / "data"), "--now", now_first_check_of_day],
    )

    output = json.loads(result.stdout)
    assert output["exits"] == []


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


def test_check_hourly_signals_trend_filter_suppresses_normal_exit(tmp_path):
    cfg = {
        **BASE_CFG,
        "stochastic": {"k_period": 3, "k_smooth": 1, "d_period": 2, "oversold_threshold": 20, "overbought_threshold": 80},
        "trend_filter": {"enabled": True, "sma_period": 3},
    }
    config_path = tmp_path / "strategy.yaml"
    with config_path.open("w") as f:
        yaml.safe_dump(cfg, f)

    def bar(close):
        return {"high": 50, "low": 0, "close": close}

    # closes -> k=[.., 50, 46], d=[.., 45, 48]: prev_k(50)>=prev_d(45) and
    # cur_k(46)<cur_d(48) -> a genuine bearish crossover under STATE_NORMAL,
    # k/d both well under 80 so no OVERBOUGHT_HOLD transition either. Last
    # close (23) is above the 3-bar SMA of the last 3 closes (22.67) -> trend
    # is intact, so the crossover must be suppressed.
    closes = [10, 10, 20, 25, 23]
    result = _run(
        "check_hourly_signals.py",
        {"candidates": [], "open_positions": [{"symbol": "SHW", "entry_price": 20.0, "qty": 1.0, "bars": [bar(c) for c in closes]}]},
        ["--config", str(config_path), "--data-dir", str(tmp_path / "data")],
    )
    output = json.loads(result.stdout)
    assert output["exits"] == []
    assert output["position_states"][0]["stochastic_state"] == "NORMAL"

    events = _events(tmp_path)
    signal_check = [e for e in events if e["event"] == "signal_check" and e["kind"] == "exit"][0]
    assert signal_check["trend_intact"] is True
    assert signal_check["signal"] is False


def test_check_hourly_signals_trend_broken_allows_normal_exit(tmp_path):
    cfg = {
        **BASE_CFG,
        "stochastic": {"k_period": 3, "k_smooth": 1, "d_period": 2, "oversold_threshold": 20, "overbought_threshold": 80},
        "trend_filter": {"enabled": True, "sma_period": 3},
    }
    config_path = tmp_path / "strategy.yaml"
    with config_path.open("w") as f:
        yaml.safe_dump(cfg, f)

    def bar(close):
        return {"high": 50, "low": 0, "close": close}

    # Same crossover shape as the suppression test (k=[..,50,38], d=[..,45,44]
    # -> prev_k>=prev_d, cur_k<cur_d), but last close (19) is now BELOW the
    # 3-bar SMA (21.33) -> trend is broken, so the crossover exit must fire.
    closes = [10, 10, 20, 25, 19]
    result = _run(
        "check_hourly_signals.py",
        {"candidates": [], "open_positions": [{"symbol": "SHW", "entry_price": 20.0, "qty": 1.0, "bars": [bar(c) for c in closes]}]},
        ["--config", str(config_path), "--data-dir", str(tmp_path / "data")],
    )
    output = json.loads(result.stdout)
    assert [e["symbol"] for e in output["exits"]] == ["SHW"]
    assert output["exits"][0]["exit_reason"] == "signal_exit"

    events = _events(tmp_path)
    signal_check = [e for e in events if e["event"] == "signal_check" and e["kind"] == "exit"][0]
    assert signal_check["trend_intact"] is False
    assert signal_check["signal"] is True


def test_check_hourly_signals_trend_filter_disabled_via_config(tmp_path):
    # Same bars as the suppression test, but trend_filter.enabled: False ->
    # the crossover must fire normally, same as if the filter didn't exist.
    cfg = {
        **BASE_CFG,
        "stochastic": {"k_period": 3, "k_smooth": 1, "d_period": 2, "oversold_threshold": 20, "overbought_threshold": 80},
        "trend_filter": {"enabled": False, "sma_period": 3},
    }
    config_path = tmp_path / "strategy.yaml"
    with config_path.open("w") as f:
        yaml.safe_dump(cfg, f)

    def bar(close):
        return {"high": 50, "low": 0, "close": close}

    closes = [10, 10, 20, 25, 23]
    result = _run(
        "check_hourly_signals.py",
        {"candidates": [], "open_positions": [{"symbol": "SHW", "entry_price": 20.0, "qty": 1.0, "bars": [bar(c) for c in closes]}]},
        ["--config", str(config_path), "--data-dir", str(tmp_path / "data")],
    )
    output = json.loads(result.stdout)
    assert [e["symbol"] for e in output["exits"]] == ["SHW"]

    events = _events(tmp_path)
    signal_check = [e for e in events if e["event"] == "signal_check" and e["kind"] == "exit"][0]
    assert signal_check["trend_intact"] is None


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

    entry_closes = [10, 10, 8, 7, 15]

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
    assert output["entries"][0]["last_close"] == 15.0
    assert output["entries"][0]["estimated_stop_price"] == 12.0


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


def _events_in(data_dir):
    """Like _events, but for a data-dir that isn't tmp_path/data (e.g. the
    daily-stochastic-check track's tmp_path/data/daily)."""
    log_files = list((data_dir / "logs").glob("*.jsonl"))
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


def test_record_trade_close_carries_stochastic_detail(tmp_path):
    payload = {
        "symbol": "AAPL",
        "position": {
            "entry_price": 200.0,
            "qty": 1.0,
            "entry_time": "2026-07-29T14:30:00Z",
            "entry_order_id": "entry-1",
            "stop_price": 195.0,
            "stop_order_id": "stop-1",
            "entry_k": 24.1,
            "entry_d": 19.8,
            "entry_prev_k": 18.6,
            "entry_prev_d": 15.2,
        },
        "exit_price": 205.0,
        "exit_time": "2026-07-30T15:00:00Z",
        "exit_order_id": "exit-1",
        "exit_reason": "signal_exit",
        "exit_k": 41.0,
        "exit_d": 45.2,
        "exit_prev_k": 48.3,
        "exit_prev_d": 44.7,
    }

    result = _run("record_trade_close.py", payload, ["--data-dir", str(tmp_path / "data")])
    output = json.loads(result.stdout)

    assert output["entry_k"] == 24.1
    assert output["entry_d"] == 19.8
    assert output["entry_prev_k"] == 18.6
    assert output["entry_prev_d"] == 15.2
    assert output["exit_k"] == 41.0
    assert output["exit_d"] == 45.2
    assert output["exit_prev_k"] == 48.3
    assert output["exit_prev_d"] == 44.7


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
    assert "daily" in embedded  # daily-stochastic-check comparison tab data, even if empty


def test_build_dashboard_tolerates_malformed_nested_event_field(tmp_path):
    # Confirmed live 2026-08-14: an ad-hoc cycle_summary log call nested the
    # whole summary dict under its own "event" key (e.g. logger.log(summary_dict)
    # instead of logger.log("cycle_summary", **summary_dict)), producing
    # {"event": {"event": "cycle_summary", ...}, "timestamp": ...} -- crashed
    # _todays_event_counts with "unhashable type: dict" since it tried to use
    # the whole nested dict as a Counter/dict key. build_dashboard.py must
    # unwrap it (event["event"]) rather than crash the whole dashboard build.
    import yaml as _yaml
    from datetime import datetime, timezone

    data_dir = tmp_path / "data"
    (data_dir / "logs").mkdir(parents=True)
    config_path = tmp_path / "strategy.yaml"
    with config_path.open("w") as f:
        _yaml.safe_dump(BASE_CFG, f)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log_path = data_dir / "logs" / f"{today}.jsonl"
    log_path.write_text(
        json.dumps({"timestamp": f"{today}T14:00:00+00:00", "event": "signal_check"}) + "\n"
        + json.dumps({"timestamp": f"{today}T15:02:07+00:00", "event": {"event": "cycle_summary", "entries_taken": 0, "exits_taken": 0}}) + "\n"
    )

    output_path = tmp_path / "dist.html"
    _run("build_dashboard.py", {"account": {}, "position_prices": {}}, [
        "--config", str(config_path),
        "--data-dir", str(data_dir),
        "--dashboard-dir", str(REPO_ROOT / "dashboard"),
        "--output", str(output_path),
    ])

    html = output_path.read_text()
    data_marker = 'type="application/json">'
    start = html.index(data_marker) + len(data_marker)
    end = html.index("</script>", start)
    embedded = json.loads(html[start:end])
    assert embedded["activity_today"]["signal_check"] == 1
    assert embedded["activity_today"]["cycle_summary"] == 1


def test_build_dashboard_daily_section_absent_when_no_daily_track_yet(tmp_path):
    # No config/strategy_daily.yaml, no data/daily/ -- must degrade to an
    # empty daily section, not crash the whole dashboard build.
    import yaml as _yaml

    data_dir = tmp_path / "data"
    (data_dir / "logs").mkdir(parents=True)
    config_path = tmp_path / "strategy.yaml"
    with config_path.open("w") as f:
        _yaml.safe_dump(BASE_CFG, f)

    output_path = tmp_path / "dist.html"
    _run("build_dashboard.py", {"account": {}, "position_prices": {}}, [
        "--config", str(config_path),
        "--data-dir", str(data_dir),
        "--dashboard-dir", str(REPO_ROOT / "dashboard"),
        "--output", str(output_path),
        "--daily-config", str(tmp_path / "nonexistent_strategy_daily.yaml"),
        "--daily-data-dir", str(tmp_path / "nonexistent_daily_data"),
    ])
    html = output_path.read_text()
    data_marker = 'type="application/json">'
    start = html.index(data_marker) + len(data_marker)
    end = html.index("</script>", start)
    embedded = json.loads(html[start:end])
    assert embedded["daily"] == {"positions": [], "trade_history": [], "config": {"max_positions": 0}, "available": False}


def test_build_dashboard_daily_section_populated(tmp_path):
    import yaml as _yaml

    data_dir = tmp_path / "data"
    (data_dir / "logs").mkdir(parents=True)
    config_path = tmp_path / "strategy.yaml"
    with config_path.open("w") as f:
        _yaml.safe_dump(BASE_CFG, f)

    daily_config_path = tmp_path / "strategy_daily.yaml"
    with daily_config_path.open("w") as f:
        _yaml.safe_dump({**BASE_CFG, "sizing": {**BASE_CFG["sizing"], "max_positions": 7}}, f)

    daily_data_dir = tmp_path / "daily_data"
    daily_data_dir.mkdir()
    (daily_data_dir / "positions.json").write_text(json.dumps({
        "AAPL": {"entry_price": 200.0, "qty": 1, "entry_time": "2026-08-13T00:00:00Z",
                  "entry_order_id": "paper", "stop_order_id": None, "stop_price": 195.0, "stochastic_state": "NORMAL"},
    }))
    (daily_data_dir / "trade_history.json").write_text(json.dumps([
        {"symbol": "MSFT", "qty": 1, "entry_price": 400.0, "exit_price": 410.0,
         "pnl_usd": 10.0, "pnl_pct": 2.5, "exit_reason": "signal_exit",
         "entry_time": "2026-08-11T00:00:00Z", "exit_time": "2026-08-12T00:00:00Z",
         "entry_order_id": "paper", "exit_order_id": "paper", "stop_price": 390.0, "closed_at": "2026-08-12T00:00:05Z"},
    ]))

    output_path = tmp_path / "dist.html"
    _run("build_dashboard.py", {"account": {}, "position_prices": {}}, [
        "--config", str(config_path),
        "--data-dir", str(data_dir),
        "--dashboard-dir", str(REPO_ROOT / "dashboard"),
        "--output", str(output_path),
        "--daily-config", str(daily_config_path),
        "--daily-data-dir", str(daily_data_dir),
    ])
    html = output_path.read_text()
    data_marker = 'type="application/json">'
    start = html.index(data_marker) + len(data_marker)
    end = html.index("</script>", start)
    embedded = json.loads(html[start:end])
    assert embedded["daily"]["available"] is True
    assert embedded["daily"]["config"]["max_positions"] == 7
    assert embedded["daily"]["positions"][0]["symbol"] == "AAPL"
    assert embedded["daily"]["trade_history"][0]["symbol"] == "MSFT"


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
    # Same fixed-high/low trick as tests/test_backtest.py: closes -> dual-
    # cross entry at t4 (raw %K 16,14,30 and %D 15,22 both clear 20 on the
    # same bar, spec §3 2026-08-04 revision), then a small ATR forces an
    # immediate stop-loss at t5.
    closes = [150, 150, 244, 226, 370, 460]
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


def test_filter_entry_earnings_keeps_entries_with_no_report(tmp_path):
    payload = {
        "entries": [{"symbol": "AAPL", "k": 24.1}, {"symbol": "MSFT", "k": 30.0}],
        "earnings_by_symbol": {"AAPL": [], "MSFT": []},
    }
    result = _run("filter_entry_earnings.py", payload, ["--data-dir", str(tmp_path / "data"), "--today", "2026-08-06"])
    kept = json.loads(result.stdout)
    assert [e["symbol"] for e in kept] == ["AAPL", "MSFT"]

    log_files = list((tmp_path / "data" / "logs").glob("*.jsonl"))
    assert log_files == []  # nothing excluded -> no log event at all


def test_filter_entry_earnings_excludes_too_close(tmp_path):
    payload = {
        "entries": [{"symbol": "AAPL", "k": 24.1}, {"symbol": "MSFT", "k": 30.0}],
        # AAPL reports tomorrow BMO -> exit_date is today -> too_close.
        "earnings_by_symbol": {"AAPL": [{"date": "2026-08-07", "timing": "am"}], "MSFT": []},
    }
    result = _run("filter_entry_earnings.py", payload, ["--data-dir", str(tmp_path / "data"), "--today", "2026-08-06"])
    kept = json.loads(result.stdout)
    assert [e["symbol"] for e in kept] == ["MSFT"]

    events = _events(tmp_path)
    excl = next(e for e in events if e["event"] == "entries_earnings_excluded")
    assert excl["symbols"] == ["AAPL"]
    assert excl["reasons"] == {"AAPL": "too_close"}


def test_filter_entry_earnings_excludes_unchecked_symbol_conservatively(tmp_path):
    # AAPL's earnings fetch failed/never happened -- missing from
    # earnings_by_symbol entirely -> excluded as "unknown", not passed
    # through. This is the primary gate now, so failing closed is correct.
    payload = {
        "entries": [{"symbol": "AAPL", "k": 24.1}],
        "earnings_by_symbol": {},
    }
    result = _run("filter_entry_earnings.py", payload, ["--data-dir", str(tmp_path / "data"), "--today", "2026-08-06"])
    kept = json.loads(result.stdout)
    assert kept == []

    events = _events(tmp_path)
    excl = next(e for e in events if e["event"] == "entries_earnings_excluded")
    assert excl["reasons"] == {"AAPL": "unknown"}


def test_filter_entry_earnings_empty_entries_no_op(tmp_path):
    payload = {"entries": [], "earnings_by_symbol": {}}
    result = _run("filter_entry_earnings.py", payload, ["--data-dir", str(tmp_path / "data"), "--today", "2026-08-06"])
    assert json.loads(result.stdout) == []
    assert not (tmp_path / "data" / "logs").exists() or list((tmp_path / "data" / "logs").glob("*.jsonl")) == []


def test_filter_entry_earnings_defaults_today_when_omitted(tmp_path):
    # No --today override and no "today" in payload -- must use the real
    # current date without erroring, and still apply the filter correctly.
    payload = {"entries": [{"symbol": "AAPL"}], "earnings_by_symbol": {"AAPL": []}}
    result = _run("filter_entry_earnings.py", payload, ["--data-dir", str(tmp_path / "data")])
    assert json.loads(result.stdout) == [{"symbol": "AAPL"}]


def test_check_market_trend_true_when_trending(tmp_path):
    config_path = _write_cfg(tmp_path, {"market_filter": {"enabled": True, "symbol": "SPY", "fast_sma_period": 5, "slow_sma_period": 20, "rising_lookback_bars": 3}})
    closes = [100.0 + i * 0.5 for i in range(30)]
    payload = {"bars": [{"high": c, "low": c, "close": c} for c in closes]}

    result = _run("check_market_trend.py", payload, ["--config", str(config_path), "--data-dir", str(tmp_path / "data")])
    assert json.loads(result.stdout) == {"trend_intact": True}

    events = _events(tmp_path)
    assert events[0]["event"] == "market_trend_check"
    assert events[0]["symbol"] == "SPY"
    assert events[0]["trend_intact"] is True


def test_check_market_trend_false_when_declining(tmp_path):
    config_path = _write_cfg(tmp_path, {"market_filter": {"enabled": True, "symbol": "SPY", "fast_sma_period": 5, "slow_sma_period": 20, "rising_lookback_bars": 3}})
    closes = list(reversed([100.0 + i * 0.5 for i in range(30)]))
    payload = {"bars": [{"high": c, "low": c, "close": c} for c in closes]}

    result = _run("check_market_trend.py", payload, ["--config", str(config_path), "--data-dir", str(tmp_path / "data")])
    assert json.loads(result.stdout) == {"trend_intact": False}


def test_check_market_trend_null_with_insufficient_history(tmp_path):
    config_path = _write_cfg(tmp_path, {"market_filter": {"enabled": True, "fast_sma_period": 5, "slow_sma_period": 20, "rising_lookback_bars": 3}})
    payload = {"bars": [{"high": 100, "low": 100, "close": 100}] * 5}

    result = _run("check_market_trend.py", payload, ["--config", str(config_path), "--data-dir", str(tmp_path / "data")])
    assert json.loads(result.stdout) == {"trend_intact": None}


def test_check_market_trend_disabled_returns_true_without_evaluating(tmp_path):
    config_path = _write_cfg(tmp_path, {"market_filter": {"enabled": False}})
    # Deliberately a declining series -- if the disabled check evaluated it
    # anyway this would come back False, so this proves the toggle short-circuits.
    closes = list(reversed([100.0 + i * 0.5 for i in range(30)]))
    payload = {"bars": [{"high": c, "low": c, "close": c} for c in closes]}

    result = _run("check_market_trend.py", payload, ["--config", str(config_path), "--data-dir", str(tmp_path / "data")])
    assert json.loads(result.stdout) == {"trend_intact": True}

    events = _events(tmp_path)
    assert events[0]["enabled"] is False


def test_check_market_trend_defaults_when_section_omitted(tmp_path):
    # No "market_filter" key in config at all -- must default to enabled
    # with SPY/20/200/5, not error.
    config_path = _write_cfg(tmp_path)
    closes = [100.0 + i * 0.1 for i in range(210)]
    payload = {"bars": [{"high": c, "low": c, "close": c} for c in closes]}

    result = _run("check_market_trend.py", payload, ["--config", str(config_path), "--data-dir", str(tmp_path / "data")])
    assert json.loads(result.stdout) == {"trend_intact": True}

    events = _events(tmp_path)
    assert events[0]["symbol"] == "SPY"
    assert events[0]["fast_sma_period"] == 20
    assert events[0]["slow_sma_period"] == 200


def _write_state(tmp_path, candidates=None, pending=None, positions=None):
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    (data_dir / "candidates.json").write_text(json.dumps(candidates or []))
    (data_dir / "pending_entries.json").write_text(json.dumps(pending or {}))
    (data_dir / "positions.json").write_text(json.dumps(positions or {}))
    return data_dir


def _historicals_batch(symbols_and_bars):
    return {
        "data": {
            "results": [{"symbol": symbol, "bars": bars} for symbol, bars in symbols_and_bars.items()]
        }
    }


def test_build_entries_payload_append_then_finalize(tmp_path):
    data_dir = _write_state(
        tmp_path,
        candidates=[{"symbol": "AAA", "sector": "Tech", "atr14": 1.5}, {"symbol": "BBB", "sector": "Health", "atr14": 2.0}],
        pending={"AAA": {"k_at_cross": 22.5}},
    )
    build_file = data_dir / ".entries_candidates_build.jsonl"

    batch = _historicals_batch(
        {
            "AAA": [{"begins_at": "t1", "high_price": "10", "low_price": "9", "close_price": "9.5"}],
            "BBB": [{"begins_at": "t1", "high_price": "20", "low_price": "19", "close_price": "19.5"}],
        }
    )
    _run(
        "build_entries_payload.py",
        batch,
        ["--append", "--data-dir", str(data_dir), "--build-file", str(build_file)],
    )
    assert build_file.exists()

    result = _run(
        "build_entries_payload.py",
        None,
        ["--finalize", "--data-dir", str(data_dir), "--build-file", str(build_file)],
    )
    payload = json.loads(result.stdout)
    by_symbol = {c["symbol"]: c for c in payload["candidates"]}
    assert set(by_symbol) == {"AAA", "BBB"}
    assert by_symbol["AAA"]["sector"] == "Tech"
    assert by_symbol["AAA"]["atr14"] == 1.5
    assert by_symbol["AAA"]["pending"] == {"k_at_cross": 22.5}
    assert "pending" not in by_symbol["BBB"]
    assert by_symbol["BBB"]["bars"][0]["close"] == 19.5
    assert payload["open_positions"] == []
    assert not build_file.exists()  # finalize cleans up the scratch file


def test_build_entries_payload_appends_multiple_batches(tmp_path):
    data_dir = _write_state(tmp_path, candidates=[{"symbol": "AAA", "atr14": 1.0}, {"symbol": "CCC", "atr14": 3.0}])
    build_file = data_dir / ".entries_candidates_build.jsonl"

    for symbol in ("AAA", "CCC"):
        batch = _historicals_batch({symbol: [{"begins_at": "t1", "high_price": "1", "low_price": "1", "close_price": "1"}]})
        _run("build_entries_payload.py", batch, ["--append", "--data-dir", str(data_dir), "--build-file", str(build_file)])

    result = _run("build_entries_payload.py", None, ["--finalize", "--data-dir", str(data_dir), "--build-file", str(build_file)])
    payload = json.loads(result.stdout)
    assert {c["symbol"] for c in payload["candidates"]} == {"AAA", "CCC"}


def test_build_entries_payload_skips_interpolated_bars(tmp_path):
    data_dir = _write_state(tmp_path, candidates=[{"symbol": "AAA", "atr14": 1.0}])
    build_file = data_dir / ".entries_candidates_build.jsonl"

    batch = _historicals_batch(
        {"AAA": [{"begins_at": "t1", "high_price": "1", "low_price": "1", "close_price": "1", "interpolated": True}]}
    )
    result = _run("build_entries_payload.py", batch, ["--append", "--data-dir", str(data_dir), "--build-file", str(build_file)])
    assert "AAA" in result.stderr

    events = _events(tmp_path)
    skip_event = next(e for e in events if e["event"] == "candidate_bars_interpolated")
    assert skip_event["symbols"] == ["AAA"]

    finalize_result = _run("build_entries_payload.py", None, ["--finalize", "--data-dir", str(data_dir), "--build-file", str(build_file)])
    assert json.loads(finalize_result.stdout) == {"candidates": [], "open_positions": []}


def test_build_entries_payload_drops_symbols_already_open(tmp_path):
    data_dir = _write_state(
        tmp_path,
        candidates=[{"symbol": "AAA", "atr14": 1.0}],
        positions={"AAA": {"entry_price": 10.0, "qty": 1, "stochastic_state": "NORMAL"}},
    )
    build_file = data_dir / ".entries_candidates_build.jsonl"
    batch = _historicals_batch({"AAA": [{"begins_at": "t1", "high_price": "1", "low_price": "1", "close_price": "1"}]})
    _run("build_entries_payload.py", batch, ["--append", "--data-dir", str(data_dir), "--build-file", str(build_file)])

    result = _run("build_entries_payload.py", None, ["--finalize", "--data-dir", str(data_dir), "--build-file", str(build_file)])
    payload = json.loads(result.stdout)
    assert payload["candidates"] == []

    events = _events(tmp_path)
    dropped = next(e for e in events if e["event"] == "candidate_payload_dropped_open_position")
    assert dropped["symbols"] == ["AAA"]


def test_build_entries_payload_finalize_with_no_build_file_is_empty(tmp_path):
    data_dir = _write_state(tmp_path)
    result = _run(
        "build_entries_payload.py",
        None,
        ["--finalize", "--data-dir", str(data_dir), "--build-file", str(data_dir / ".missing.jsonl")],
    )
    assert json.loads(result.stdout) == {"candidates": [], "open_positions": []}


def test_build_entries_payload_finalize_rejects_stale_build_file(tmp_path):
    import os
    import time

    data_dir = _write_state(tmp_path, candidates=[{"symbol": "AAA", "atr14": 1.0}])
    build_file = data_dir / ".entries_candidates_build.jsonl"
    build_file.write_text(json.dumps({"symbol": "AAA", "bars": []}) + "\n")
    old_time = time.time() - 3 * 3600  # 3 hours old, past the default 120-minute limit
    os.utime(build_file, (old_time, old_time))

    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "build_entries_payload.py"), "--finalize", "--data-dir", str(data_dir), "--build-file", str(build_file)],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert result.returncode != 0
    assert "stale" in result.stderr.lower() or "minutes old" in result.stderr
    assert not build_file.exists()  # deleted despite the error, so the next cycle starts clean


def test_build_entries_payload_candidates_data_dir_reads_shared_candidates(tmp_path):
    # daily-stochastic-check usage: candidates.json lives in a shared dir,
    # but pending_entries.json/positions.json live in the track's own dir.
    shared_dir = tmp_path / "data"
    shared_dir.mkdir()
    (shared_dir / "candidates.json").write_text(json.dumps([{"symbol": "AAA", "sector": "Tech", "atr14": 1.5}]))

    daily_dir = tmp_path / "data" / "daily"
    daily_dir.mkdir()
    (daily_dir / "pending_entries.json").write_text(json.dumps({"AAA": {"k_at_cross": 22.5}}))
    (daily_dir / "positions.json").write_text(json.dumps({}))

    build_file = daily_dir / ".entries_candidates_build.jsonl"
    batch = _historicals_batch({"AAA": [{"begins_at": "t1", "high_price": "10", "low_price": "9", "close_price": "9.5"}]})
    _run("build_entries_payload.py", batch, ["--append", "--data-dir", str(daily_dir), "--build-file", str(build_file)])

    result = _run(
        "build_entries_payload.py",
        None,
        [
            "--finalize",
            "--data-dir", str(daily_dir),
            "--candidates-data-dir", str(shared_dir),
            "--build-file", str(build_file),
        ],
    )
    payload = json.loads(result.stdout)
    assert len(payload["candidates"]) == 1
    c = payload["candidates"][0]
    assert c["symbol"] == "AAA"
    assert c["sector"] == "Tech"
    assert c["atr14"] == 1.5
    assert c["pending"] == {"k_at_cross": 22.5}  # pending still comes from --data-dir, not --candidates-data-dir


def test_check_paper_stops_detects_low_breach(tmp_path):
    payload = {
        "positions": [
            {"symbol": "AAA", "stop_price": 95.0, "bars": [{"begins_at": "2026-08-13T00:00:00Z", "high": 101.0, "low": 94.0, "close": 96.0}]},
            {"symbol": "BBB", "stop_price": 50.0, "bars": [{"begins_at": "2026-08-13T00:00:00Z", "high": 60.0, "low": 55.0, "close": 58.0}]},
        ]
    }
    result = _run("check_paper_stops.py", payload, ["--data-dir", str(tmp_path / "data")])
    out = json.loads(result.stdout)
    assert out == {"stop_outs": [{"symbol": "AAA", "exit_price": 95.0, "exit_time": "2026-08-13T00:00:00Z"}]}

    events = _events(tmp_path)
    assert events[0]["event"] == "paper_stop_out"
    assert events[0]["symbol"] == "AAA"


def test_check_paper_stops_no_breach_no_events(tmp_path):
    payload = {
        "positions": [
            {"symbol": "AAA", "stop_price": 90.0, "bars": [{"begins_at": "2026-08-13T00:00:00Z", "high": 101.0, "low": 94.0, "close": 96.0}]},
        ]
    }
    result = _run("check_paper_stops.py", payload, ["--data-dir", str(tmp_path / "data")])
    assert json.loads(result.stdout) == {"stop_outs": []}
    assert not (tmp_path / "data" / "logs").exists() or list((tmp_path / "data" / "logs").glob("*.jsonl")) == []


def test_check_paper_stops_skips_symbol_with_no_bars(tmp_path):
    payload = {"positions": [{"symbol": "AAA", "stop_price": 90.0, "bars": []}]}
    result = _run("check_paper_stops.py", payload, ["--data-dir", str(tmp_path / "data")])
    assert json.loads(result.stdout) == {"stop_outs": []}


def test_record_paper_entry_writes_position(tmp_path):
    data_dir = tmp_path / "data" / "daily"
    payload = {
        "symbol": "AAA",
        "entry_price": 100.0,
        "qty": 2,
        "entry_time": "2026-08-13T00:00:00Z",
        "stop_price": 95.0,
        "entry_k": 24.1,
        "entry_d": 19.8,
        "entry_prev_k": 18.6,
        "entry_prev_d": 15.2,
    }
    result = _run("record_paper_entry.py", payload, ["--data-dir", str(data_dir)])
    out = json.loads(result.stdout)
    assert out["symbol"] == "AAA"
    assert out["entry_order_id"] == "paper"
    assert out["stop_order_id"] is None
    assert out["stochastic_state"] == "NORMAL"
    assert out["stop_price"] == 95.0
    assert out["entry_k"] == 24.1

    positions = json.loads((data_dir / "positions.json").read_text())
    assert "AAA" in positions
    assert positions["AAA"]["qty"] == 2
    assert positions["AAA"]["entry_order_id"] == "paper"

    log_files = list((data_dir / "logs").glob("*.jsonl"))
    assert len(log_files) == 1
    events = [json.loads(line) for line in log_files[0].read_text().splitlines()]
    assert events[0]["event"] == "paper_entry_recorded"
    assert events[0]["symbol"] == "AAA"


def test_record_paper_entry_overwrites_existing_symbol(tmp_path):
    data_dir = tmp_path / "data" / "daily"
    data_dir.mkdir(parents=True)
    (data_dir / "positions.json").write_text(json.dumps({"BBB": {"entry_price": 1.0, "qty": 1}}))

    payload = {
        "symbol": "AAA",
        "entry_price": 50.0,
        "qty": 1,
        "entry_time": "2026-08-13T00:00:00Z",
        "stop_price": 48.0,
    }
    _run("record_paper_entry.py", payload, ["--data-dir", str(data_dir)])
    positions = json.loads((data_dir / "positions.json").read_text())
    assert set(positions.keys()) == {"AAA", "BBB"}
    assert positions["AAA"]["entry_k"] is None  # omitted fields default to null, not a KeyError


def test_queue_paper_fill_writes_pending_fills(tmp_path):
    data_dir = tmp_path / "data" / "daily"
    payload = {
        "symbol": "AAA",
        "atr14": 2.0,
        "entry_k": 24.1,
        "entry_d": 19.8,
        "entry_prev_k": 18.6,
        "entry_prev_d": 15.2,
        "signal_date": "2026-08-14",
    }
    result = _run("queue_paper_fill.py", payload, ["--data-dir", str(data_dir)])
    out = json.loads(result.stdout)
    assert out["symbol"] == "AAA"
    assert out["atr14"] == 2.0

    pending = json.loads((data_dir / "pending_fills.json").read_text())
    assert pending["AAA"]["entry_k"] == 24.1
    assert pending["AAA"]["signal_date"] == "2026-08-14"

    events = _events_in(data_dir)
    assert events[0]["event"] == "paper_fill_queued"


def test_queue_paper_fill_overwrites_existing_pending_symbol(tmp_path):
    data_dir = tmp_path / "data" / "daily"
    data_dir.mkdir(parents=True)
    (data_dir / "pending_fills.json").write_text(json.dumps({"AAA": {"atr14": 1.0}, "BBB": {"atr14": 3.0}}))

    payload = {"symbol": "AAA", "atr14": 5.0}
    _run("queue_paper_fill.py", payload, ["--data-dir", str(data_dir)])
    pending = json.loads((data_dir / "pending_fills.json").read_text())
    assert pending["AAA"]["atr14"] == 5.0
    assert pending["BBB"]["atr14"] == 3.0


def _write_daily_cfg(tmp_path):
    return _write_cfg(tmp_path, {"atr": {"period": 14, "stop_multiplier": 1.5}})


def test_settle_paper_fills_fills_at_next_open(tmp_path):
    data_dir = tmp_path / "data" / "daily"
    data_dir.mkdir(parents=True)
    (data_dir / "pending_fills.json").write_text(json.dumps({
        "AAA": {"atr14": 2.0, "entry_k": 24.1, "entry_d": 19.8, "entry_prev_k": 18.6, "entry_prev_d": 15.2, "signal_date": "2026-08-13"},
    }))
    config_path = _write_daily_cfg(tmp_path)

    payload = {"bars_by_symbol": {"AAA": [{"begins_at": "2026-08-14T00:00:00Z", "open": 100.0, "high": 102.0, "low": 99.0, "close": 101.0}]}}
    result = _run("settle_paper_fills.py", payload, ["--config", str(config_path), "--data-dir", str(data_dir)])
    out = json.loads(result.stdout)
    assert out["still_pending"] == []
    assert len(out["filled"]) == 1
    assert out["filled"][0]["symbol"] == "AAA"
    assert out["filled"][0]["entry_price"] == 100.0
    assert out["filled"][0]["stop_price"] == 100.0 - 1.5 * 2.0

    positions = json.loads((data_dir / "positions.json").read_text())
    assert positions["AAA"]["entry_price"] == 100.0
    assert positions["AAA"]["entry_order_id"] == "paper"
    assert positions["AAA"]["stop_order_id"] is None
    assert positions["AAA"]["stochastic_state"] == "NORMAL"
    assert positions["AAA"]["entry_k"] == 24.1

    pending_after = json.loads((data_dir / "pending_fills.json").read_text())
    assert pending_after == {}

    events = _events_in(data_dir)
    filled_event = next(e for e in events if e["event"] == "paper_entry_filled_next_open")
    assert filled_event["symbol"] == "AAA"
    assert filled_event["signal_date"] == "2026-08-13"


def test_settle_paper_fills_leaves_symbol_pending_when_no_fresh_bar(tmp_path):
    data_dir = tmp_path / "data" / "daily"
    data_dir.mkdir(parents=True)
    (data_dir / "pending_fills.json").write_text(json.dumps({
        "AAA": {"atr14": 2.0},
        "BBB": {"atr14": 1.0},
    }))
    config_path = _write_daily_cfg(tmp_path)

    payload = {"bars_by_symbol": {"AAA": [{"begins_at": "2026-08-14T00:00:00Z", "open": 100.0, "high": 102.0, "low": 99.0, "close": 101.0}]}}
    result = _run("settle_paper_fills.py", payload, ["--config", str(config_path), "--data-dir", str(data_dir)])
    out = json.loads(result.stdout)
    assert out["still_pending"] == ["BBB"]
    assert [f["symbol"] for f in out["filled"]] == ["AAA"]

    pending_after = json.loads((data_dir / "pending_fills.json").read_text())
    assert set(pending_after.keys()) == {"BBB"}

    positions = json.loads((data_dir / "positions.json").read_text())
    assert set(positions.keys()) == {"AAA"}


def test_settle_paper_fills_drops_symbol_over_price_cap(tmp_path):
    data_dir = tmp_path / "data" / "daily"
    data_dir.mkdir(parents=True)
    (data_dir / "pending_fills.json").write_text(json.dumps({"AAA": {"atr14": 2.0}}))
    config_path = _write_daily_cfg(tmp_path)  # BASE_CFG's max_price_per_share is 150

    payload = {"bars_by_symbol": {"AAA": [{"begins_at": "2026-08-14T00:00:00Z", "open": 500.0, "high": 505.0, "low": 495.0, "close": 500.0}]}}
    result = _run("settle_paper_fills.py", payload, ["--config", str(config_path), "--data-dir", str(data_dir)])
    out = json.loads(result.stdout)
    assert out["filled"] == []
    assert out["still_pending"] == []  # dropped, not retried

    pending_after = json.loads((data_dir / "pending_fills.json").read_text())
    assert pending_after == {}
    positions = json.loads((data_dir / "positions.json").read_text())
    assert positions == {}

    events = _events_in(data_dir)
    assert events[0]["event"] == "paper_fill_skipped_price_cap"


def test_build_entries_payload_excludes_pending_fill_symbols(tmp_path):
    data_dir = _write_state(tmp_path, candidates=[{"symbol": "AAA", "atr14": 1.0}, {"symbol": "BBB", "atr14": 2.0}])
    (data_dir / "pending_fills.json").write_text(json.dumps({"AAA": {"atr14": 1.0}}))
    build_file = data_dir / ".entries_candidates_build.jsonl"

    for symbol in ("AAA", "BBB"):
        batch = _historicals_batch({symbol: [{"begins_at": "t1", "high_price": "1", "low_price": "1", "close_price": "1"}]})
        _run("build_entries_payload.py", batch, ["--append", "--data-dir", str(data_dir), "--build-file", str(build_file)])

    result = _run("build_entries_payload.py", None, ["--finalize", "--data-dir", str(data_dir), "--build-file", str(build_file)])
    payload = json.loads(result.stdout)
    assert {c["symbol"] for c in payload["candidates"]} == {"BBB"}  # AAA excluded, already pending a fill

