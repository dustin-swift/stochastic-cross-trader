#!/usr/bin/env python3
"""Portfolio-level daily-loss circuit breaker check (spec §5, §6b). Thin CLI
wrapping lib.risk.circuit_breaker_tripped — alerts and logs if tripped. The
hourly-signal-check skill calls this each cycle before evaluating any new
entries. Strategy-agnostic (see lib.config.load_raw_config's docstring): the
MA Pullback / Breakout-Retest agent's hourly skill calls this exact same
script too, just pointed at its own --config/--data-dir.

Input (JSON, via --input file or stdin):
  {"account_value": 1500.0, "realized_pnl_today": -10.0, "unrealized_pnl_today": -20.0}

Output (JSON, to stdout): {"tripped": bool}
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.alerts import send_alert
from lib.config import load_raw_config
from lib.logging_utils import EventLogger
from lib.risk import circuit_breaker_tripped


def _load_input(input_arg: str) -> dict:
    if input_arg == "-":
        return json.load(sys.stdin)
    with open(input_arg) as f:
        return json.load(f)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default="-", help="JSON input file, or '-' for stdin (default)")
    parser.add_argument("--config", default="config/strategy.yaml")
    parser.add_argument("--data-dir", default="data")
    args = parser.parse_args()

    config = load_raw_config(args.config)
    payload = _load_input(args.input)
    logger = EventLogger(args.data_dir)

    account_value = payload["account_value"]
    realized = payload["realized_pnl_today"]
    unrealized = payload["unrealized_pnl_today"]
    max_daily_loss_pct = config["risk"]["max_daily_loss_pct"]

    tripped = circuit_breaker_tripped(account_value, realized, unrealized, max_daily_loss_pct)

    logger.log(
        "circuit_breaker_check",
        tripped=tripped,
        account_value=account_value,
        realized_pnl_today=realized,
        unrealized_pnl_today=unrealized,
        max_daily_loss_pct=max_daily_loss_pct,
    )

    if tripped:
        message = (
            f"Circuit breaker tripped: realized {realized} + unrealized {unrealized} "
            f"vs account value {account_value} (max_daily_loss_pct={max_daily_loss_pct}). "
            "No new entries this cycle."
        )
        logger.log("circuit_breaker_triggered", message=message)
        send_alert("circuit_breaker_triggered", message)

    json.dump({"tripped": tripped}, sys.stdout)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
