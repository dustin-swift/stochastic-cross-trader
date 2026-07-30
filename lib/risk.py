"""Portfolio-level daily-loss circuit breaker. Pure function — the hourly
skill supplies account value, today's realized P&L (get_realized_pnl,
span=day), and today's unrealized P&L (from get_portfolio / open positions).

When tripped, no *new* entries are opened for the rest of the day; existing
exit/stop-out handling is unaffected (see hourly-signal-check skill).
"""
from __future__ import annotations


def circuit_breaker_tripped(
    account_value: float,
    realized_pnl_today: float,
    unrealized_pnl_today: float,
    max_daily_loss_pct: float,
) -> bool:
    """True when today's combined realized + unrealized P&L has dropped to or
    below -max_daily_loss_pct% of account_value.

    An account_value of 0 (unfunded, or a funding transfer not yet settled)
    is not an error case — there's nothing to size positions against, so this
    trips the breaker (blocks new entries) rather than raising. A *negative*
    account_value would be a genuinely malformed input and still raises.
    """
    if account_value < 0:
        raise ValueError("account_value must not be negative")
    if account_value == 0:
        return True
    if max_daily_loss_pct <= 0:
        raise ValueError("max_daily_loss_pct must be positive")

    total_pnl_today = realized_pnl_today + unrealized_pnl_today
    loss_threshold = -max_daily_loss_pct / 100 * account_value

    return total_pnl_today <= loss_threshold
