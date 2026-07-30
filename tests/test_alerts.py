from unittest.mock import MagicMock, patch

from lib.alerts import send_alert

SLACK_CFG = {"alerts": {"provider": "slack"}}
DISCORD_CFG = {"alerts": {"provider": "discord"}}


def _ok_response():
    resp = MagicMock()
    resp.status_code = 200
    return resp


def test_send_alert_slack_payload_shape(monkeypatch):
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://hooks.slack.example/abc")
    with patch("lib.alerts.requests.post", return_value=_ok_response()) as mock_post:
        send_alert("circuit_breaker_triggered", "tripped at -3%", config=SLACK_CFG)

    mock_post.assert_called_once()
    args, kwargs = mock_post.call_args
    assert args[0] == "https://hooks.slack.example/abc"
    assert kwargs["json"] == {"text": "[circuit_breaker_triggered] tripped at -3%"}


def test_send_alert_discord_payload_shape(monkeypatch):
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://discord.example/webhook")
    with patch("lib.alerts.requests.post", return_value=_ok_response()) as mock_post:
        send_alert("stop_placement_failed", "AAPL stop failed twice", config=DISCORD_CFG)

    args, kwargs = mock_post.call_args
    assert kwargs["json"] == {"content": "[stop_placement_failed] AAPL stop failed twice"}


def test_send_alert_defaults_to_slack_shape_for_unknown_provider(monkeypatch):
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://hooks.slack.example/abc")
    cfg = {"alerts": {"provider": "carrier_pigeon"}}
    with patch("lib.alerts.requests.post", return_value=_ok_response()) as mock_post:
        send_alert("universe_screen_empty", "0 candidates", config=cfg)

    args, kwargs = mock_post.call_args
    assert "text" in kwargs["json"]


def test_send_alert_no_webhook_url_does_not_call_post_or_raise(monkeypatch):
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
    with patch("lib.alerts.requests.post") as mock_post:
        send_alert("universe_screen_blocked", "stale csv", config=SLACK_CFG)
    mock_post.assert_not_called()


def test_send_alert_swallows_network_exception(monkeypatch):
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://hooks.slack.example/abc")
    with patch("lib.alerts.requests.post", side_effect=ConnectionError("boom")):
        send_alert("entry_order_rejected_or_timeout", "AAPL rejected", config=SLACK_CFG)
    # no exception propagated -> test passes by reaching here


def test_send_alert_swallows_non_2xx_response(monkeypatch):
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://hooks.slack.example/abc")
    resp = MagicMock()
    resp.status_code = 500
    resp.text = "internal error"
    with patch("lib.alerts.requests.post", return_value=resp):
        send_alert("circuit_breaker_triggered", "tripped", config=SLACK_CFG)
    # no exception propagated -> test passes by reaching here


def test_send_alert_missing_config_dict_loads_real_config(monkeypatch):
    # No config passed -> falls back to load_config() against the real
    # config/strategy.yaml (run from repo root, matching other tests' pattern).
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://hooks.slack.example/abc")
    with patch("lib.alerts.requests.post", return_value=_ok_response()) as mock_post:
        send_alert("universe_screen_empty", "0 candidates")
    mock_post.assert_called_once()
