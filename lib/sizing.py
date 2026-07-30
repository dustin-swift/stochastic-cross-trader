"""Whole-share entry sizing (2026-07-30 fix).

Dollar-based fractional-share entries can't get a resting protective stop
placed at all: confirmed live, twice (XLF, JHX), that this broker rejects
*any* stop-type trigger (stop_market, stop_limit) on a fractional-share
quantity ("Invalid trigger for fractional order"), independent of
time_in_force. Since the whole system's safety model depends on a real
resting stop existing after every entry, entries must size to a whole
number of shares instead.

Pure function, no network calls -- the hourly-signal-check skill calls this
with the candidate's last bar close (already fetched) before deciding
whether/how to place the entry order.
"""
from __future__ import annotations


def entry_share_quantity(price: float, per_trade_usd: float, max_price_per_share: float) -> int | None:
    """Whole-share entry quantity:
      - price < per_trade_usd: round(per_trade_usd / price) shares -- the
        whole-share count landing closest to `per_trade_usd`, whichever side.
      - per_trade_usd <= price <= max_price_per_share: exactly 1 share.
      - price > max_price_per_share: None -- skip the entry entirely rather
        than buying 1 share regardless of cost.
    `max_price_per_share` is expected to be set high enough above
    `per_trade_usd` (e.g. 1.5x) that round() never produces 0 for any price
    this function doesn't already skip.
    """
    if price <= 0:
        raise ValueError(f"price must be positive, got {price}")
    if price > max_price_per_share:
        return None
    return max(1, round(per_trade_usd / price))
