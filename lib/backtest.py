"""Historical backtest engine — NOT part of the live/dry-run pipeline; this is
a standalone research tool for answering "how would this strategy have
performed?" against already-fetched historical data.

Replays the exact same entry/exit logic used live (lib.signals) bar-by-bar
across a chronologically merged multi-symbol timeline, simulating the
$100/trade, max-N-concurrent-positions sizing (config["sizing"]) and the ATR
stop (config["atr"]) from config/strategy.yaml. Pure computation, no network
calls — callers fetch historical bars + an ATR series via Robinhood MCP
(get_equity_historicals / get_equity_technical_indicators) and feed them in.

Fill model (v1, deliberately simple): perfect fills at the signal bar's close
for entries and signal exits; a stop-loss fills at the exact stop price on the
first subsequent bar whose low touches or breaches it. No slippage or fees are
modeled — results will read slightly optimistic versus live trading.

Optional trailing-stop research variant (config["trailing_stop"], disabled by
default — see config/strategy.yaml): once a position's running high reaches
`activation_pct` above entry, its effective stop starts trailing
`atr_multiplier` x current ATR behind that running high, ratcheting up only
— it can never loosen back below the original fixed ATR stop. This is purely
a backtest research toggle; the live pipeline (scripts/check_hourly_signals.py)
doesn't read this config section and always uses the fixed entry-time stop.

Earnings/catalyst avoidance (config["catalysts"], mirrors lib.catalysts and
scripts/check_hourly_signals.py so the backtest stays a faithful simulator of
the live behavior): if a symbol's `earnings_report_dates` is provided in
symbol_data, entries are skipped on any bar within `earnings_exclusion_days`
of a report, and open positions are force-exited (exit_reason
"earnings_exit", at that bar's close) within `earnings_forced_exit_days` —
checked before the stop/trailing-stop check each bar, since it's a scheduled,
date-driven decision, not a reactive one. A symbol with no
`earnings_report_dates` key is never restricted (backtest default is "don't
know, don't filter" — unlike the live daily-screen's conservative "unknown ->
exclude," since a missing key here almost always just means the caller didn't
fetch earnings for that symbol, not a real gap to be cautious about).

Multi-symbol tie-breaking: when more candidates signal an entry in the same
bar than there are open slots, symbols are processed in alphabetical order.
This is a backtest-only modeling choice (the live system takes entries "in
the order returned" by the signal-check script) — there is no principled way
to reconstruct real-time priority after the fact from bar data alone.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from lib.catalysts import is_too_close_to_earnings
from lib.indicators import stochastic
from lib.signals import (
    STATE_NORMAL,
    STATE_OVERBOUGHT_HOLD,
    entry_signal,
    exit_signal,
    stop_price,
    update_stochastic_state,
)


@dataclass
class Trade:
    symbol: str
    entry_time: str
    entry_price: float
    qty: float
    stop_price: float
    exit_time: str | None = None
    exit_price: float | None = None
    exit_reason: str | None = None  # stop_loss | signal_exit | overbought_hold_exit | open_at_end
    # Max favorable/adverse excursion: the best/worst price seen on any bar
    # from the bar AFTER entry through exit (or through the last available
    # bar, for a still-open position). None until at least one post-entry bar
    # has been processed. Purely observational -- doesn't affect exit
    # decisions, just answers "how far did this run before it did whatever it
    # did," which the entry/exit prices alone can't show (e.g. whether a
    # trailing stop would plausibly have helped).
    mfe_price: float | None = None
    mae_price: float | None = None

    @property
    def pnl(self) -> float | None:
        if self.exit_price is None:
            return None
        return (self.exit_price - self.entry_price) * self.qty

    @property
    def pnl_pct(self) -> float | None:
        if self.exit_price is None:
            return None
        return (self.exit_price - self.entry_price) / self.entry_price * 100

    @property
    def mfe_pct(self) -> float | None:
        if self.mfe_price is None:
            return None
        return (self.mfe_price - self.entry_price) / self.entry_price * 100

    @property
    def mae_pct(self) -> float | None:
        if self.mae_price is None:
            return None
        return (self.mae_price - self.entry_price) / self.entry_price * 100

    def update_excursion(self, bar_high: float, bar_low: float) -> None:
        self.mfe_price = bar_high if self.mfe_price is None else max(self.mfe_price, bar_high)
        self.mae_price = bar_low if self.mae_price is None else min(self.mae_price, bar_low)

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "entry_time": self.entry_time,
            "entry_price": self.entry_price,
            "qty": self.qty,
            "stop_price": self.stop_price,
            "exit_time": self.exit_time,
            "exit_price": self.exit_price,
            "exit_reason": self.exit_reason,
            "pnl": self.pnl,
            "pnl_pct": self.pnl_pct,
            "mfe_pct": self.mfe_pct,
            "mae_pct": self.mae_pct,
        }


@dataclass
class _OpenPosition:
    trade: Trade
    stochastic_state: str = STATE_NORMAL
    # The stop level actually in force this bar. Starts at trade.stop_price
    # (the fixed ATR stop) and, when trailing is enabled, only ever ratchets
    # upward — never below trade.stop_price, so trailing can only ever help,
    # not widen risk versus the baseline fixed stop. Set explicitly by the
    # caller at creation (see run_backtest) rather than defaulted here, since
    # it must start equal to trade.stop_price.
    effective_stop: float = float("nan")


def _prep_symbol(bars: list[dict], atr_series: list[dict], stoch_cfg: dict) -> tuple[pd.DataFrame, dict[str, int]]:
    """One DataFrame per symbol: float-coerced high/low/close, precomputed
    %K/%D, and ATR aligned by `begins_at` timestamp — plus a timestamp->row
    index lookup so the main loop doesn't rescan the frame per bar.
    """
    df = pd.DataFrame(bars)
    for col in ("high", "low", "close"):
        df[col] = df[col].astype(float)

    sdf = stochastic(
        df,
        k_period=stoch_cfg["k_period"],
        k_smooth=stoch_cfg["k_smooth"],
        d_period=stoch_cfg["d_period"],
    )
    df["k"] = sdf["k"]
    df["d"] = sdf["d"]

    atr_by_time = {a["begins_at"]: a["value"] for a in atr_series}
    df["atr14"] = df["begins_at"].map(atr_by_time)

    time_to_idx = {t: i for i, t in enumerate(df["begins_at"])}
    return df, time_to_idx


def run_backtest(symbol_data: dict[str, dict], config: dict) -> dict:
    """
    symbol_data: {
      "AAPL": {
        "bars": [{"begins_at": "...", "high": .., "low": .., "close": ..}, ...],  # oldest-first
        "atr_series": [{"begins_at": "...", "value": ..}, ...]
      }, ...
    }
    config: same shape as config/strategy.yaml — uses the stochastic, atr, and
    sizing sections. `stochastic.entry_lookback_bars` is optional (defaults to
    1, matching lib.signals.entry_signal's default) so older configs without
    the key still work.
    """
    stoch_cfg = config["stochastic"]
    oversold = stoch_cfg["oversold_threshold"]
    overbought = stoch_cfg["overbought_threshold"]
    lookback_bars = stoch_cfg.get("entry_lookback_bars", 1)
    atr_mult = config["atr"]["stop_multiplier"]
    per_trade_usd = config["sizing"]["per_trade_usd"]
    max_positions = config["sizing"]["max_positions"]

    trailing_cfg = config.get("trailing_stop", {})
    trailing_enabled = trailing_cfg.get("enabled", False)
    trailing_activation_pct = trailing_cfg.get("activation_pct", 3.0)
    trailing_atr_mult = trailing_cfg.get("atr_multiplier", 2.0)

    catalysts_cfg = config.get("catalysts", {})
    earnings_exclusion_days = catalysts_cfg.get("earnings_exclusion_days", 5)
    earnings_forced_exit_days = catalysts_cfg.get("earnings_forced_exit_days", 1)
    earnings_by_symbol = {
        symbol: data["earnings_report_dates"] for symbol, data in symbol_data.items() if "earnings_report_dates" in data
    }

    prepped = {
        symbol: _prep_symbol(data["bars"], data.get("atr_series", []), stoch_cfg)
        for symbol, data in symbol_data.items()
    }

    # Sort by parsed timestamp, not raw string -- a lexical sort of ISO8601
    # UTC strings (what Robinhood's get_equity_historicals actually returns)
    # happens to work, but relying on that implicitly is fragile and silently
    # wrong (not an error) for anything else, so parse explicitly.
    all_times = sorted({t for _, time_to_idx in prepped.values() for t in time_to_idx}, key=pd.Timestamp)

    open_positions: dict[str, _OpenPosition] = {}
    closed_trades: list[Trade] = []

    for t in all_times:
        symbols_at_t = sorted(s for s, (_, idx) in prepped.items() if t in idx)

        # -- exits first: stop-loss, then signal/overbought-hold exit -------
        for symbol in symbols_at_t:
            if symbol not in open_positions:
                continue
            df, time_to_idx = prepped[symbol]
            i = time_to_idx[t]
            bar = df.iloc[i]
            pos = open_positions[symbol]

            pos.trade.update_excursion(float(bar["high"]), float(bar["low"]))

            if symbol in earnings_by_symbol and is_too_close_to_earnings(
                earnings_by_symbol[symbol], pd.Timestamp(t).date(), earnings_forced_exit_days
            ):
                pos.trade.exit_time = t
                pos.trade.exit_price = float(bar["close"])
                pos.trade.exit_reason = "earnings_exit"
                closed_trades.append(pos.trade)
                del open_positions[symbol]
                continue

            if trailing_enabled:
                atr_now = bar["atr14"]
                gain_pct = (pos.trade.mfe_price - pos.trade.entry_price) / pos.trade.entry_price * 100
                if gain_pct >= trailing_activation_pct and not pd.isna(atr_now):
                    trail_level = pos.trade.mfe_price - trailing_atr_mult * float(atr_now)
                    pos.effective_stop = max(pos.effective_stop, trail_level)

            if bar["low"] <= pos.effective_stop:
                pos.trade.exit_time = t
                pos.trade.exit_price = pos.effective_stop
                pos.trade.exit_reason = (
                    "trailing_stop" if pos.effective_stop > pos.trade.stop_price else "stop_loss"
                )
                closed_trades.append(pos.trade)
                del open_positions[symbol]
                continue

            sdf_upto = df.iloc[: i + 1][["k", "d"]]
            new_state = update_stochastic_state(pos.stochastic_state, sdf_upto, overbought_threshold=overbought)
            pos.stochastic_state = new_state
            if exit_signal(sdf_upto, state=new_state, overbought_threshold=overbought):
                pos.trade.exit_time = t
                pos.trade.exit_price = float(bar["close"])
                pos.trade.exit_reason = (
                    "overbought_hold_exit" if new_state == STATE_OVERBOUGHT_HOLD else "signal_exit"
                )
                closed_trades.append(pos.trade)
                del open_positions[symbol]

        # -- entries ----------------------------------------------------------
        for symbol in symbols_at_t:
            if symbol in open_positions:
                continue
            if len(open_positions) >= max_positions:
                break

            if symbol in earnings_by_symbol and is_too_close_to_earnings(
                earnings_by_symbol[symbol], pd.Timestamp(t).date(), earnings_exclusion_days
            ):
                continue

            df, time_to_idx = prepped[symbol]
            i = time_to_idx[t]
            bar = df.iloc[i]

            sdf_upto = df.iloc[: i + 1][["k", "d"]]
            if not entry_signal(sdf_upto, oversold_threshold=oversold, lookback_bars=lookback_bars):
                continue

            atr14 = bar["atr14"]
            if pd.isna(atr14):
                continue  # can't size a stop without ATR at this bar -- skip

            close = float(bar["close"])
            qty = per_trade_usd / close
            stop = stop_price(close, float(atr14), mult=atr_mult)
            trade = Trade(symbol=symbol, entry_time=t, entry_price=close, qty=qty, stop_price=stop)
            open_positions[symbol] = _OpenPosition(trade=trade, stochastic_state=STATE_NORMAL, effective_stop=stop)

    # Anything still open at the end of the window is reported separately --
    # not a win, not a loss, just unresolved as of the last available bar.
    open_trades = []
    for pos in open_positions.values():
        pos.trade.exit_reason = "open_at_end"
        open_trades.append(pos.trade)

    return {
        "trades": [t.to_dict() for t in closed_trades],
        "open_positions": [t.to_dict() for t in open_trades],
        "summary": _summarize(closed_trades),
    }


def _summarize(trades: list[Trade]) -> dict:
    n = len(trades)
    if n == 0:
        return {
            "trade_count": 0,
            "win_count": 0,
            "loss_count": 0,
            "win_rate_pct": None,
            "total_pnl": 0.0,
            "avg_pnl": None,
            "avg_win": None,
            "avg_loss": None,
            "max_drawdown": 0.0,
            "by_exit_reason": {},
        }

    pnls = [t.pnl for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        max_dd = min(max_dd, cum - peak)

    by_reason: dict[str, int] = {}
    for t in trades:
        by_reason[t.exit_reason] = by_reason.get(t.exit_reason, 0) + 1

    return {
        "trade_count": n,
        "win_count": len(wins),
        "loss_count": len(losses),
        "win_rate_pct": len(wins) / n * 100,
        "total_pnl": sum(pnls),
        "avg_pnl": sum(pnls) / n,
        "avg_win": (sum(wins) / len(wins)) if wins else None,
        "avg_loss": (sum(losses) / len(losses)) if losses else None,
        "max_drawdown": max_dd,
        "by_exit_reason": by_reason,
    }
