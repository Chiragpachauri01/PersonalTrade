"""P&L analytics (ROADMAP M12): realized P&L (from `Trade.realized_pnl`, M11)
plus unrealized (mark-to-market on open positions), an equity curve
reconstructed from Trade cash flows, and trade-level statistics.

Reuses backtest/metrics.py's equity-series statistics (`cagr`/`sharpe_ratio`/
`max_drawdown`) unchanged — they already operate on the generic `EquitySeries`
type, no backtest-specific coupling. `win_rate`/`expectancy`/`profit_factor`
are re-derived here over a plain P&L list rather than imported, since
backtest's versions are coupled to its own `ExecutedTrade` dataclass and the
three functions are a few lines each — a shared abstraction across two
different trade-record shapes isn't worth it yet (Rule 5).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from personaltrade.backtest.metrics import EquitySeries, cagr, max_drawdown, sharpe_ratio
from personaltrade.core.enums import Interval, Mode, Side
from personaltrade.data.store.candles import CandleStore
from personaltrade.data.store.models import Trade
from personaltrade.data.store.repos import InstrumentRepository, PositionRepository


def win_rate(pnls: Sequence[float]) -> float:
    if not pnls:
        return 0.0
    return sum(1 for p in pnls if p > 0) / len(pnls)


def expectancy(pnls: Sequence[float]) -> float:
    """Average realized P&L per closed round-trip trade, in rupees."""
    return sum(pnls) / len(pnls) if pnls else 0.0


def profit_factor(pnls: Sequence[float]) -> float:
    """Gross wins / gross losses. inf if there are wins and no losses; 0 if no wins."""
    gross_win = sum(p for p in pnls if p > 0)
    gross_loss = -sum(p for p in pnls if p < 0)
    if gross_loss == 0:
        return float("inf") if gross_win > 0 else 0.0
    return gross_win / gross_loss


def equity_curve_from_trades(initial_cash: Decimal, trades: Sequence[Trade]) -> EquitySeries:
    """Cash-only step function, ascending by `executed_at` — changes at each
    fill. Deliberately doesn't mark open positions at each historical point
    (that would need a historical price at arbitrary past timestamps, which
    nothing in this codebase indexes); only `unrealized_pnl` below marks the
    CURRENT open positions. Most trade journals present exactly this — a
    realized equity curve that steps at fills, not a continuous
    mark-to-market — so this is a deliberate scope choice, not an oversight.
    """
    ordered = sorted(trades, key=lambda t: t.executed_at)
    if not ordered:
        return []
    cash = initial_cash
    series: list[tuple[datetime, float]] = [(ordered[0].executed_at, float(cash))]
    for trade in ordered:
        cash += trade.net_amount if trade.order.side == Side.SELL else -trade.net_amount
        series.append((trade.executed_at, float(cash)))
    return series


def unrealized_pnl(
    session: Session, candle_store: CandleStore, mode: Mode, interval: Interval
) -> Decimal:
    """Mark every open position to the last *synced* candle close (the same
    stand-in reference price ADR-019's `ReplayQuoteSource` uses) — this is a
    point-in-time report, not a live feed, so there's no live tick to mark
    against. Positions for an instrument with no synced candles are skipped
    (contribute 0), not estimated."""
    total = Decimal("0")
    positions = PositionRepository(session).list_open(mode)
    instruments = InstrumentRepository(session)
    for position in positions:
        instrument = instruments.get(position.instrument_id)
        if instrument is None:
            continue
        frame = candle_store.read(instrument.symbol, instrument.exchange, interval)
        if frame.empty:
            continue
        mark = Decimal(str(frame["close"].iloc[-1]))
        total += (mark - position.avg_price) * position.qty
    return total


@dataclass(frozen=True)
class PnLSummary:
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    total_pnl: Decimal
    win_rate: float
    expectancy: float
    profit_factor: float
    closed_trades: int
    cagr: float
    sharpe: float
    max_drawdown: float


def compute_pnl_summary(
    initial_cash: Decimal,
    realized_trades: Sequence[Trade],
    all_trades: Sequence[Trade],
    unrealized: Decimal,
) -> PnLSummary:
    """`realized_trades` (closing legs only, `realized_pnl is not None`) drives
    win_rate/expectancy/profit_factor; `all_trades` (every leg) drives the
    equity curve, since opening legs move cash too."""
    pnls = [float(t.realized_pnl) for t in realized_trades if t.realized_pnl is not None]
    realized = sum(
        (t.realized_pnl for t in realized_trades if t.realized_pnl is not None), Decimal("0")
    )
    series = equity_curve_from_trades(initial_cash, all_trades)
    return PnLSummary(
        realized_pnl=realized,
        unrealized_pnl=unrealized,
        total_pnl=realized + unrealized,
        win_rate=win_rate(pnls),
        expectancy=expectancy(pnls),
        profit_factor=profit_factor(pnls),
        closed_trades=len(pnls),
        cagr=cagr(series),
        sharpe=sharpe_ratio(series),
        max_drawdown=max_drawdown(series),
    )
