"""Entry-order fill lifecycle decisions (spec §6b amendment). Pure function
over an already-fetched order status — no network calls. The hourly-signal-
check skill polls get_equity_orders and feeds each poll's result through
this to decide whether to keep waiting, proceed to stop placement, or bail
out (rejected / timed out unfilled).
"""
from __future__ import annotations

_TERMINAL_FAILURE_STATES = {"rejected", "cancelled", "failed", "voided"}


def evaluate_fill_status(
    order_state: str,
    filled_qty: float,
    requested_qty: float,
    elapsed_seconds: float,
    timeout_seconds: float,
) -> dict:
    """Decide what to do with an in-flight entry order given its latest
    polled state.

    Returns {"decision": "wait"|"proceed"|"rejected"|"timeout",
             "filled_qty": float, "needs_cancel": bool}.

    - A terminal failure state (rejected/cancelled/failed/voided) -> "rejected":
      no position should be written, nothing left to cancel (already terminal).
    - Fully filled -> "proceed" with the full filled_qty.
    - Partially filled: keep waiting under timeout (it may still complete);
      once the timeout is reached, "proceed" with whatever quantity did fill
      (using that as the position) and needs_cancel=True for the unfilled
      remainder still resting at the broker.
    - Any other open state (new/queued/confirmed/unconfirmed) past timeout
      with nothing filled -> "timeout": no position, needs_cancel=True.
    """
    if order_state in _TERMINAL_FAILURE_STATES:
        return {"decision": "rejected", "filled_qty": 0.0, "needs_cancel": False}

    if order_state == "filled":
        return {"decision": "proceed", "filled_qty": filled_qty, "needs_cancel": False}

    timed_out = elapsed_seconds >= timeout_seconds

    if order_state == "partially_filled":
        if not timed_out:
            return {"decision": "wait", "filled_qty": filled_qty, "needs_cancel": False}
        if filled_qty > 0:
            return {"decision": "proceed", "filled_qty": filled_qty, "needs_cancel": True}
        return {"decision": "timeout", "filled_qty": 0.0, "needs_cancel": True}

    # new / queued / confirmed / unconfirmed / anything else still open
    if timed_out:
        return {"decision": "timeout", "filled_qty": 0.0, "needs_cancel": True}
    return {"decision": "wait", "filled_qty": filled_qty, "needs_cancel": False}
