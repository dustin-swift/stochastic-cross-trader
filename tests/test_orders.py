import pytest

from lib.orders import evaluate_fill_status


def test_filled_proceeds_with_full_quantity():
    result = evaluate_fill_status("filled", filled_qty=0.5, requested_qty=0.5, elapsed_seconds=3, timeout_seconds=30)
    assert result == {"decision": "proceed", "filled_qty": 0.5, "needs_cancel": False}


@pytest.mark.parametrize("state", ["rejected", "cancelled", "failed", "voided"])
def test_terminal_failure_states_are_rejected(state):
    result = evaluate_fill_status(state, filled_qty=0, requested_qty=0.5, elapsed_seconds=5, timeout_seconds=30)
    assert result == {"decision": "rejected", "filled_qty": 0.0, "needs_cancel": False}


@pytest.mark.parametrize("state", ["new", "queued", "confirmed", "unconfirmed"])
def test_open_states_wait_before_timeout(state):
    result = evaluate_fill_status(state, filled_qty=0, requested_qty=0.5, elapsed_seconds=10, timeout_seconds=30)
    assert result == {"decision": "wait", "filled_qty": 0, "needs_cancel": False}


@pytest.mark.parametrize("state", ["new", "queued", "confirmed", "unconfirmed"])
def test_open_states_timeout_when_unfilled(state):
    result = evaluate_fill_status(state, filled_qty=0, requested_qty=0.5, elapsed_seconds=30, timeout_seconds=30)
    assert result == {"decision": "timeout", "filled_qty": 0.0, "needs_cancel": True}


def test_partially_filled_waits_before_timeout():
    result = evaluate_fill_status(
        "partially_filled", filled_qty=0.2, requested_qty=0.5, elapsed_seconds=10, timeout_seconds=30
    )
    assert result == {"decision": "wait", "filled_qty": 0.2, "needs_cancel": False}


def test_partially_filled_past_timeout_proceeds_with_filled_qty_and_cancels_remainder():
    result = evaluate_fill_status(
        "partially_filled", filled_qty=0.2, requested_qty=0.5, elapsed_seconds=31, timeout_seconds=30
    )
    assert result == {"decision": "proceed", "filled_qty": 0.2, "needs_cancel": True}


def test_partially_filled_past_timeout_with_zero_filled_is_timeout():
    # Edge case: state says partially_filled but nothing has actually landed yet.
    result = evaluate_fill_status(
        "partially_filled", filled_qty=0, requested_qty=0.5, elapsed_seconds=31, timeout_seconds=30
    )
    assert result == {"decision": "timeout", "filled_qty": 0.0, "needs_cancel": True}


def test_elapsed_exactly_at_timeout_counts_as_timed_out():
    result = evaluate_fill_status("new", filled_qty=0, requested_qty=0.5, elapsed_seconds=30, timeout_seconds=30)
    assert result["decision"] == "timeout"
