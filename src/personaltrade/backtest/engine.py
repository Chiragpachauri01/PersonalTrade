"""Event-driven backtest engine: single instrument, one strategy, no look-ahead.

Signal at bar i fills at bar i+1's open, adjusted by adverse slippage, with
the full Indian cost stack applied (Rule 12 — pessimistic simulation). At
most one position open at a time; a signal that would reverse position
directly (LONG while SHORT, or vice versa) is ignored with a warning — a
strategy must emit EXIT first. A signal emitted on the final bar has no next
bar to fill on and is recorded as unfilled, not executed.

`avg_price` always includes that leg's own transaction costs (folded into
the per-share cost basis at open), so `ExecutedTrade.realized_pnl` on a
closing trade is the true, complete round-trip P&L — what a trader actually
cares about for win-rate/expectancy, not just the exit leg in isolation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

import pandas as pd

from personaltrade.backtest.costs import TradeCosts, calculate_costs
from personaltrade.backtest.indicator_bridge import (
    BatchIndicatorView,
    compute_indicator_set,
    first_all_valid_index,
)
from personaltrade.backtest.sizing import PositionSizer
from personaltrade.core.config import CostConfig
from personaltrade.core.enums import Segment, Side, SignalDirection
from personaltrade.core.errors import PersonalTradeError
from personaltrade.core.logging import get_logger
from personaltrade.strategy.base import (
    FLAT_POSITION,
    PositionView,
    Signal,
    Strategy,
    StrategyContext,
)

logger = get_logger(__name__)


class BacktestError(PersonalTradeError):
    """The backtest could not run (empty data, invalid params, etc.)."""


@dataclass(frozen=True)
class ExecutedTrade:
    index: int  # bar index the fill happened on (signal_index + 1)
    ts: datetime
    side: Side
    qty: int
    price: Decimal  # fill price, post-slippage
    costs: TradeCosts
    signal_index: int  # bar index the originating signal was emitted on
    realized_pnl: Decimal | None  # set only on closing trades (full round-trip P&L)


@dataclass(frozen=True)
class EquityPoint:
    index: int
    ts: datetime
    cash: Decimal
    position_qty: int
    position_value: Decimal
    equity: Decimal


@dataclass(frozen=True)
class UnfilledSignal:
    """A signal with no next bar to fill on (emitted on the final bar)."""

    index: int
    ts: datetime
    direction: SignalDirection


@dataclass(frozen=True)
class BacktestResult:
    trades: list[ExecutedTrade] = field(default_factory=list)
    equity_curve: list[EquityPoint] = field(default_factory=list)
    unfilled_signals: list[UnfilledSignal] = field(default_factory=list)
    final_position: PositionView = FLAT_POSITION


@dataclass
class _Portfolio:
    """Internal mutable ledger. Decimal throughout; avg_price includes costs."""

    cash: Decimal
    qty: int = 0
    avg_price: Decimal = Decimal("0")

    def equity(self, mark_price: Decimal) -> Decimal:
        return self.cash + Decimal(self.qty) * mark_price

    def as_view(self) -> PositionView:
        if self.qty == 0:
            return FLAT_POSITION
        return PositionView(qty=self.qty, avg_price=float(self.avg_price))


def run_backtest(
    strategy: Strategy,
    candles: pd.DataFrame,
    *,
    initial_capital: Decimal,
    sizer: PositionSizer,
    cost_rates: CostConfig,
    segment: Segment = Segment.DELIVERY,
    slippage_bps: Decimal = Decimal("5"),
) -> BacktestResult:
    """Replay `candles` through `strategy`, one instrument, next-bar fills.

    `candles` must already satisfy the provider-neutral frame contract (UTC,
    ascending, unique — see personaltrade.data.providers.base), the same
    invariant CandleStore.read() guarantees.
    """
    if candles.empty:
        raise BacktestError("cannot backtest an empty candle series")
    if initial_capital <= 0:
        raise BacktestError(f"initial_capital must be > 0, got {initial_capital}")

    n = len(candles)
    candles = candles.reset_index(drop=True)
    indicator_series = compute_indicator_set(candles, strategy.required_indicators())
    effective_start = max(strategy.warmup_bars(), first_all_valid_index(indicator_series))

    portfolio = _Portfolio(cash=initial_capital)
    trades: list[ExecutedTrade] = []
    equity_curve: list[EquityPoint] = []
    unfilled: list[UnfilledSignal] = []
    pending: Signal | None = None
    pending_index: int | None = None

    for i in range(n):
        ts = candles["ts"].iloc[i].to_pydatetime()

        if pending is not None:
            assert pending_index is not None
            raw_open = Decimal(str(candles["open"].iloc[i]))
            trade = _execute_signal(
                pending,
                pending_index,
                i,
                ts,
                raw_open,
                portfolio,
                sizer,
                cost_rates,
                segment,
                slippage_bps,
            )
            if trade is not None:
                trades.append(trade)
            pending = None
            pending_index = None

        close_price = Decimal(str(candles["close"].iloc[i]))
        equity_curve.append(
            EquityPoint(
                index=i,
                ts=ts,
                cash=portfolio.cash,
                position_qty=portfolio.qty,
                position_value=Decimal(portfolio.qty) * close_price,
                equity=portfolio.equity(close_price),
            )
        )

        if i < effective_start:
            continue

        ctx = StrategyContext(
            index=i,
            ts=ts,
            candles=candles.iloc[: i + 1],
            indicators=BatchIndicatorView(indicator_series, i),
            position=portfolio.as_view(),
        )
        signal = strategy.on_candle(ctx)
        if signal is None:
            continue
        if i == n - 1:
            unfilled.append(UnfilledSignal(index=i, ts=ts, direction=signal.direction))
            continue
        pending = signal
        pending_index = i

    return BacktestResult(
        trades=trades,
        equity_curve=equity_curve,
        unfilled_signals=unfilled,
        final_position=portfolio.as_view(),
    )


def _resolve_action(current_qty: int, direction: SignalDirection) -> tuple[Side, bool] | None:
    """Returns (side, is_closing) for a valid transition, or None if the signal is a no-op
    (already positioned that way, already flat on EXIT, or an unsupported direct reversal —
    a strategy wanting to reverse must emit EXIT on one bar and the new direction later).
    """
    is_flat = current_qty == 0
    is_long = current_qty > 0
    is_short = current_qty < 0

    if direction == SignalDirection.LONG:
        return (Side.BUY, False) if is_flat else None
    if direction == SignalDirection.SHORT:
        return (Side.SELL, False) if is_flat else None
    if direction == SignalDirection.EXIT:
        if is_long:
            return (Side.SELL, True)
        if is_short:
            return (Side.BUY, True)
        return None
    return None


def _apply_slippage(price: Decimal, side: Side, slippage_bps: Decimal) -> Decimal:
    factor = slippage_bps / Decimal(10000)
    return price * (Decimal(1) + factor) if side == Side.BUY else price * (Decimal(1) - factor)


def _clamp_to_cash(
    qty: int, fill_price: Decimal, segment: Segment, rates: CostConfig, cash: Decimal
) -> int:
    """Reduce qty until a BUY of that size (incl. costs) fits in available cash."""
    while qty > 0:
        if calculate_costs(Side.BUY, fill_price, qty, segment, rates).net_amount <= cash:
            return qty
        qty -= 1
    return 0


def _open_or_add(
    portfolio: _Portfolio, side: Side, qty: int, net_amount: Decimal, fill_price: Decimal
) -> None:
    signed_qty = qty if side == Side.BUY else -qty
    cost_basis_per_share = net_amount / qty
    if portfolio.qty == 0:
        portfolio.qty = signed_qty
        portfolio.avg_price = cost_basis_per_share
    else:
        total_cost = portfolio.avg_price * abs(portfolio.qty) + cost_basis_per_share * qty
        portfolio.qty += signed_qty
        portfolio.avg_price = total_cost / abs(portfolio.qty)
    portfolio.cash += net_amount if side == Side.SELL else -net_amount


def _close(portfolio: _Portfolio, side: Side, qty: int, net_amount: Decimal) -> Decimal:
    """Full-position close (M6 never partially exits). Returns realized_pnl."""
    if side == Side.SELL:  # closing a long
        realized = net_amount - portfolio.avg_price * qty
        portfolio.cash += net_amount
    else:  # BUY, covering a short
        realized = portfolio.avg_price * qty - net_amount
        portfolio.cash -= net_amount
    portfolio.qty = 0
    portfolio.avg_price = Decimal("0")
    return realized


def _execute_signal(
    signal: Signal,
    signal_index: int,
    fill_index: int,
    fill_ts: datetime,
    raw_open_price: Decimal,
    portfolio: _Portfolio,
    sizer: PositionSizer,
    cost_rates: CostConfig,
    segment: Segment,
    slippage_bps: Decimal,
) -> ExecutedTrade | None:
    action = _resolve_action(portfolio.qty, signal.direction)
    if action is None:
        logger.warning(
            "signal_ignored_invalid_transition",
            position_qty=portfolio.qty,
            direction=str(signal.direction),
            signal_index=signal_index,
        )
        return None
    side, closing = action

    if closing:
        qty = abs(portfolio.qty)
    else:
        qty = sizer.size(portfolio.equity(raw_open_price), raw_open_price)
    if qty <= 0:
        return None

    fill_price = _apply_slippage(raw_open_price, side, slippage_bps)

    if side == Side.BUY:
        qty = _clamp_to_cash(qty, fill_price, segment, cost_rates, portfolio.cash)
        if qty <= 0:
            return None

    costs = calculate_costs(side, fill_price, qty, segment, cost_rates)

    realized_pnl: Decimal | None = None
    if closing:
        realized_pnl = _close(portfolio, side, qty, costs.net_amount)
    else:
        _open_or_add(portfolio, side, qty, costs.net_amount, fill_price)

    return ExecutedTrade(
        index=fill_index,
        ts=fill_ts,
        side=side,
        qty=qty,
        price=fill_price,
        costs=costs,
        signal_index=signal_index,
        realized_pnl=realized_pnl,
    )
