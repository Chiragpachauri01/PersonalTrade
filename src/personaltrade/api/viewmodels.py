"""Read-only view models shared by the HTML pages, the REST API, and the
websocket snapshot (ROADMAP M16) — one place computing "the current state of
the world," so all three surfaces can never disagree. Every function here
only reads; nothing places, sizes, or cancels an order (CLAUDE.md Rule 10).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from personaltrade.analytics.journal import JournalEntry
from personaltrade.analytics.reports import Breakdown, PerformanceReport
from personaltrade.core.config import AppConfig
from personaltrade.core.enums import Interval
from personaltrade.data.store.candles import CandleStore
from personaltrade.data.store.repos import InstrumentRepository, RecommendationRepository
from personaltrade.execution.broker import Funds
from personaltrade.execution.paper.broker import PaperBroker
from personaltrade.execution.paper.quotes import ReplayQuoteSource
from personaltrade.risk.kill_switch import KillSwitch


@dataclass(frozen=True)
class PositionRow:
    symbol: str
    qty: int
    avg_price: Decimal


@dataclass(frozen=True)
class KillSwitchStatus:
    tripped: bool
    reason: str | None
    tripped_at: datetime | None
    consecutive_errors: int


@dataclass(frozen=True)
class AccountSnapshot:
    funds: Funds
    positions: list[PositionRow]
    kill_switch: KillSwitchStatus


def build_paper_broker(cfg: AppConfig, session: Session, store: CandleStore) -> PaperBroker:
    """Same construction as `pt paper status` (cli.py) — reused, not
    reimplemented, so the dashboard and CLI can never disagree about funds/
    positions math."""
    interval = Interval(cfg.trading.interval)
    quotes = ReplayQuoteSource(store, interval)
    return PaperBroker(
        session, quotes, cost_rates=cfg.costs, paper_cfg=cfg.paper, initial_cash=cfg.risk.capital
    )


def account_snapshot(cfg: AppConfig, session: Session, store: CandleStore) -> AccountSnapshot:
    broker = build_paper_broker(cfg, session, store)
    funds = broker.get_funds()
    instruments = InstrumentRepository(session)
    positions = []
    for p in broker.get_positions():
        inst = instruments.get(p.instrument_id)
        symbol = inst.symbol if inst is not None else f"instrument#{p.instrument_id}"
        positions.append(PositionRow(symbol=symbol, qty=p.qty, avg_price=p.avg_price))

    state = KillSwitch(session).state()
    kill_switch = KillSwitchStatus(
        tripped=state.tripped,
        reason=state.reason,
        tripped_at=state.tripped_at,
        consecutive_errors=state.consecutive_errors,
    )
    return AccountSnapshot(funds=funds, positions=positions, kill_switch=kill_switch)


def account_snapshot_json(snapshot: AccountSnapshot) -> dict[str, object]:
    """The JSON shape for `/api/account` and the `/ws/live` push — one
    serializer so both surfaces stay byte-for-byte identical."""
    return {
        "funds": {"cash": str(snapshot.funds.cash), "equity": str(snapshot.funds.equity)},
        "positions": [
            {"symbol": p.symbol, "qty": p.qty, "avg_price": str(p.avg_price)}
            for p in snapshot.positions
        ],
        "kill_switch": {
            "tripped": snapshot.kill_switch.tripped,
            "reason": snapshot.kill_switch.reason,
            "tripped_at": (
                snapshot.kill_switch.tripped_at.isoformat()
                if snapshot.kill_switch.tripped_at is not None
                else None
            ),
            "consecutive_errors": snapshot.kill_switch.consecutive_errors,
        },
    }


def default_report_interval(cfg: AppConfig) -> Interval:
    return Interval(cfg.trading.interval)


@dataclass(frozen=True)
class RecommendationRow:
    rank: int
    symbol: str
    action: str
    rationale: dict[str, object]
    created_at: datetime


def latest_recommendations(session: Session) -> list[RecommendationRow]:
    instruments = InstrumentRepository(session)
    rows = []
    for rec in RecommendationRepository(session).list_latest_cycle():
        inst = instruments.get(rec.instrument_id)
        symbol = inst.symbol if inst is not None else f"instrument#{rec.instrument_id}"
        rows.append(
            RecommendationRow(
                rank=rec.rank,
                symbol=symbol,
                action=rec.action.value,
                rationale=rec.rationale,
                created_at=rec.created_at,
            )
        )
    return rows


def recommendation_row_json(row: RecommendationRow) -> dict[str, object]:
    return {
        "rank": row.rank,
        "symbol": row.symbol,
        "action": row.action,
        "rationale": row.rationale,
        "created_at": row.created_at.isoformat(),
    }


def journal_entry_json(entry: JournalEntry) -> dict[str, object]:
    return {
        "symbol": entry.symbol,
        "side": entry.side.value,
        "qty": entry.qty,
        "entry_price": str(entry.entry_price),
        "entry_at": entry.entry_at.isoformat(),
        "exit_price": str(entry.exit_price),
        "exit_at": entry.exit_at.isoformat(),
        "realized_pnl": str(entry.realized_pnl),
        "total_costs": str(entry.total_costs),
    }


def report_json(report: PerformanceReport) -> dict[str, object]:
    s = report.summary

    def _breakdown(items: list[Breakdown]) -> list[dict[str, object]]:
        return [
            {
                "label": b.label,
                "realized_pnl": str(b.realized_pnl),
                "closed_trades": b.closed_trades,
                "win_rate": b.win_rate,
            }
            for b in items
        ]

    return {
        "since": report.since.isoformat(),
        "summary": {
            "realized_pnl": str(s.realized_pnl),
            "unrealized_pnl": str(s.unrealized_pnl),
            "total_pnl": str(s.total_pnl),
            "win_rate": s.win_rate,
            "expectancy": s.expectancy,
            "profit_factor": s.profit_factor,
            "closed_trades": s.closed_trades,
            "cagr": s.cagr,
            "sharpe": s.sharpe,
            "max_drawdown": s.max_drawdown,
        },
        "by_instrument": _breakdown(report.by_instrument),
        "by_strategy": _breakdown(report.by_strategy),
        "journal": [journal_entry_json(e) for e in report.journal],
    }


