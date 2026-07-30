"""Minimal out-of-band alerting for critical unattended-failure events. A log
line alone won't reach anyone in real time when this runs on a schedule — this
posts to a Slack or Discord incoming webhook instead. Deliberately narrow:
only the handful of call sites documented in the plan call this, not routine
activity, to avoid alert fatigue.

Never raises: a failed alert send must not take down the trading pipeline —
any error (network, bad config, missing webhook URL) is caught and logged
locally as a warning instead.
"""
from __future__ import annotations

import os
import sys

import requests

from lib.config import load_config

_PAYLOAD_BUILDERS = {
    "slack": lambda message: {"text": message},
    "discord": lambda message: {"content": message},
}


def send_alert(event_type: str, message: str, config: dict | None = None) -> None:
    """POST `message` to the configured webhook, tagged with `event_type`.

    `config` is accepted for testability (pass a fixture dict directly);
    real callers should omit it and let this load config/strategy.yaml.
    """
    try:
        cfg = config if config is not None else load_config()
        provider = cfg.get("alerts", {}).get("provider", "slack")
        webhook_url = os.environ.get("ALERT_WEBHOOK_URL")

        if not webhook_url:
            print(
                f"[alerts] ALERT_WEBHOOK_URL not set — dropping alert "
                f"(event={event_type}): {message}",
                file=sys.stderr,
            )
            return

        build_payload = _PAYLOAD_BUILDERS.get(provider, _PAYLOAD_BUILDERS["slack"])
        payload = build_payload(f"[{event_type}] {message}")

        response = requests.post(webhook_url, json=payload, timeout=10)
        if response.status_code >= 400:
            print(
                f"[alerts] webhook returned {response.status_code} for event={event_type}: "
                f"{response.text[:200]}",
                file=sys.stderr,
            )
    except Exception as exc:  # noqa: BLE001 - alerting must never crash the caller
        print(f"[alerts] failed to send alert (event={event_type}): {exc}", file=sys.stderr)
