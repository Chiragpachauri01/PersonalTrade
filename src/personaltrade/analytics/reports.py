"""Daily/weekly performance reports (ROADMAP M12): combines pnl.py's P&L
summary with per-instrument and per-strategy breakdowns and the trade journal
for a date range — one call for each `pt report` CLI command to make.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from personaltrade.analytics.journal import JournalEntry, build_journal
from personaltrade.analytics.pnl import PnLSummary, compute_pnl_summary, unrealized_pnl
from personaltrade.core.enums import Interval, Mode
from personaltrade.data.store.candles import CandleStore
from personaltrade.data.store.models import Trade
from personaltrade.data.store.repos import TradeRepository

_UNATTRIBUTED = "(unattributed)"  # a Trade whose Order predates the M12 Signal/StrategyRun retrofit


@dataclass(frozen=True)
class Breakdown:
    label: str
    realized_pnl: Decimal
    closed_trades: int
    win_rate: float


@dataclass(frozen=True)
class PerformanceReport:
    since: datetime
    summary: PnLSummary
    by_instrument: list[Breakdown]
    by_strategy: list[Breakdown]
    journal: list[JournalEntry]


def _breakdown_by(trades_by_label: dict[str, list[Trade]]) -> list[Breakdown]:
    result = []
    for label, trades in trades_by_label.items():
        pnls = [float(t.realized_pnl) for t in trades if t.realized_pnl is not None]
        realized = sum((t.realized_pnl for t in trades if t.realized_pnl is not None), Decimal("0"))
        win_rate = sum(1 for p in pnls if p > 0) / len(pnls) if pnls else 0.0
        result.append(
            Breakdown(
                label=label, realized_pnl=realized, closed_trades=len(pnls), win_rate=win_rate
            )
        )
    result.sort(key=lambda b: b.realized_pnl, reverse=True)
    return result


def generate_report(
    session: Session,
    candle_store: CandleStore,
    *,
    mode: Mode,
    initial_cash: Decimal,
    interval: Interval,
    since: datetime,
) -> PerformanceReport:
    trade_repo = TradeRepository(session)
    realized_trades = trade_repo.list_realized_since(mode, since)
    all_trades = trade_repo.list_for_mode(mode)
    unrealized = unrealized_pnl(session, candle_store, mode, interval)
    summary = compute_pnl_summary(initial_cash, realized_trades, all_trades, unrealized)

    by_instrument: dict[str, list[Trade]] = defaultdict(list)
    by_strategy: dict[str, list[Trade]] = defaultdict(list)
    for trade in realized_trades:
        order = trade.order
        by_instrument[order.instrument.symbol].append(trade)
        strategy_name = order.signal.strategy_run.strategy_name if order.signal else _UNATTRIBUTED
        by_strategy[strategy_name].append(trade)

    return PerformanceReport(
        since=since,
        summary=summary,
        by_instrument=_breakdown_by(by_instrument),
        by_strategy=_breakdown_by(by_strategy),
        journal=build_journal(session, mode, since=since),
    )
