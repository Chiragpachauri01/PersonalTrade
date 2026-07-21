"""Repositories — the only sanctioned way for other modules to touch the state store.

A generic CRUD base covers the simple tables; tables with invariants (orders,
positions, news) get dedicated repositories that enforce them.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from personaltrade.core.enums import (
    ALLOWED_ORDER_TRANSITIONS,
    Mode,
    OrderState,
)
from personaltrade.core.errors import PersonalTradeError
from personaltrade.data.store.models import (
    AIAnalysis,
    BacktestRun,
    Base,
    Instrument,
    KillSwitchState,
    NewsItem,
    Order,
    OrderEvent,
    PaperAccount,
    Position,
    Recommendation,
    RiskEvent,
    Signal,
    StrategyRun,
    Trade,
)


class InvalidOrderTransition(PersonalTradeError):
    """Attempted an order state transition the state machine forbids."""


class SqlRepository[M: Base]:
    """Minimal CRUD over one mapped class."""

    model: type[M]

    def __init__(self, session: Session) -> None:
        self.session = session

    def add(self, obj: M) -> M:
        self.session.add(obj)
        self.session.flush()
        return obj

    def get(self, id_: int) -> M | None:
        return self.session.get(self.model, id_)

    def list_all(self) -> list[M]:
        return list(self.session.scalars(select(self.model)).all())


class InstrumentRepository(SqlRepository[Instrument]):
    model = Instrument

    def get_by_symbol(self, symbol: str, exchange: str = "NSE") -> Instrument | None:
        stmt = select(Instrument).where(
            Instrument.symbol == symbol, Instrument.exchange == exchange
        )
        return self.session.scalars(stmt).one_or_none()

    def get_by_instrument_key(self, instrument_key: str) -> Instrument | None:
        stmt = select(Instrument).where(Instrument.instrument_key == instrument_key)
        return self.session.scalars(stmt).one_or_none()


class OrderRepository(SqlRepository[Order]):
    model = Order

    def get_by_client_order_id(self, client_order_id: str) -> Order | None:
        stmt = select(Order).where(Order.client_order_id == client_order_id)
        return self.session.scalars(stmt).one_or_none()

    def list_open(self, mode: Mode) -> list[Order]:
        open_states = (
            OrderState.SUBMITTING,
            OrderState.SUBMITTED,
            OrderState.OPEN,
            OrderState.PARTIALLY_FILLED,
        )
        stmt = select(Order).where(Order.state.in_(open_states), Order.mode == mode)
        return list(self.session.scalars(stmt).all())

    def record_created(self, order: Order) -> Order:
        """Persist a new order plus its birth event (None -> PENDING_RISK)."""
        self.add(order)
        self.session.add(OrderEvent(order_id=order.id, from_state=None, to_state=order.state))
        self.session.flush()
        return order

    def transition(
        self, order: Order, to_state: OrderState, payload: dict[str, Any] | None = None
    ) -> Order:
        """Move an order through the state machine, appending the audit event (ADR-007)."""
        allowed = ALLOWED_ORDER_TRANSITIONS[order.state]
        if to_state not in allowed:
            raise InvalidOrderTransition(
                f"order {order.client_order_id}: {order.state} -> {to_state} not allowed"
            )
        event = OrderEvent(
            order_id=order.id,
            from_state=order.state,
            to_state=to_state,
            payload=payload or {},
        )
        order.state = to_state
        self.session.add(event)
        self.session.flush()
        return order


class PositionRepository(SqlRepository[Position]):
    model = Position

    def get_for(self, instrument_id: int, mode: Mode) -> Position | None:
        stmt = select(Position).where(
            Position.instrument_id == instrument_id, Position.mode == mode
        )
        return self.session.scalars(stmt).one_or_none()

    def get_or_create(self, instrument_id: int, mode: Mode) -> Position:
        existing = self.get_for(instrument_id, mode)
        if existing is not None:
            return existing
        return self.add(Position(instrument_id=instrument_id, mode=mode))

    def count_open(self, mode: Mode) -> int:
        """Positions with a non-zero (long or short) quantity, for max_open_positions."""
        stmt = select(Position).where(Position.mode == mode, Position.qty != 0)
        return len(self.session.scalars(stmt).all())

    def list_open(self, mode: Mode) -> list[Position]:
        stmt = select(Position).where(Position.mode == mode, Position.qty != 0)
        return list(self.session.scalars(stmt).all())


class NewsRepository(SqlRepository[NewsItem]):
    model = NewsItem

    def add_if_new(self, item: NewsItem) -> NewsItem | None:
        """Insert unless an item with the same URL exists (dedup). Returns None if duplicate."""
        stmt = select(NewsItem.id).where(NewsItem.url == item.url)
        if self.session.scalars(stmt).first() is not None:
            return None
        return self.add(item)


class SignalRepository(SqlRepository[Signal]):
    model = Signal


class TradeRepository(SqlRepository[Trade]):
    model = Trade

    def sum_realized_pnl_since(self, mode: Mode, since: datetime) -> Decimal:
        """Sum of closing-leg realized P&L since `since` (ROADMAP M11 daily-loss
        risk check). Opening/adding legs have `realized_pnl is None` and are
        excluded — only closes ever realize anything (ADR-018/ADR-019)."""
        stmt = (
            select(Trade)
            .join(Order, Trade.order_id == Order.id)
            .where(Order.mode == mode, Trade.executed_at >= since, Trade.realized_pnl.is_not(None))
        )
        total = Decimal("0")
        for trade in self.session.scalars(stmt).all():
            assert trade.realized_pnl is not None
            total += trade.realized_pnl
        return total


class StrategyRunRepository(SqlRepository[StrategyRun]):
    model = StrategyRun


class RiskEventRepository(SqlRepository[RiskEvent]):
    model = RiskEvent


class KillSwitchStateRepository(SqlRepository[KillSwitchState]):
    model = KillSwitchState

    #: Singleton row id — one kill switch for the whole process, not per-instrument/mode.
    ROW_ID = 1

    def get_or_create(self) -> KillSwitchState:
        existing = self.get(self.ROW_ID)
        if existing is not None:
            return existing
        return self.add(KillSwitchState(id=self.ROW_ID))


class PaperAccountRepository(SqlRepository[PaperAccount]):
    model = PaperAccount

    #: Singleton row id — one paper account for the whole process, same as KillSwitchState.
    ROW_ID = 1

    def get_or_create(self, initial_cash: Decimal) -> PaperAccount:
        """`initial_cash` is required (not defaulted to 0) so a caller can't
        accidentally seed a real paper account with no starting capital by
        forgetting to pass it — only matters on first-ever call; an existing
        row's cash is never reset to this value."""
        existing = self.get(self.ROW_ID)
        if existing is not None:
            return existing
        return self.add(PaperAccount(id=self.ROW_ID, cash=initial_cash))


class AIAnalysisRepository(SqlRepository[AIAnalysis]):
    model = AIAnalysis


class RecommendationRepository(SqlRepository[Recommendation]):
    model = Recommendation


class BacktestRunRepository(SqlRepository[BacktestRun]):
    model = BacktestRun
