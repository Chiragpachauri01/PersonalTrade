"""`Broker` implementation: simulated execution against a `QuoteSource`, sharing the
backtester's exact cost model (backtest/costs.py, ADR-013) and slippage function
(ADR-015/ADR-019) — nothing upstream can tell paper fills apart from backtest fills
except that they happen one order at a time, against whatever `QuoteSource` returns,
instead of a fixed candle series.

Order lifecycle is driven synchronously inside `place_order`/`check_resting_orders` —
there is no live loop yet (that arrives with the orchestrator, M11) to drive it
otherwise. Simulated latency is a timestamp offset applied to the fill's recorded
time, not real sleeping (see ADR-019): everything here is deterministic and
restart-safe by construction, since all state lives in the existing Order/OrderEvent/
Trade/Position tables plus one new singleton `PaperAccount` row for cash — a fresh
`PaperBroker` built on the same DB after a restart sees exactly the same truth.
"""

from __future__ import annotations

from collections import deque
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from sqlalchemy.orm import Session

from personaltrade.backtest.costs import apply_slippage, calculate_costs
from personaltrade.core.clock import Clock, SystemClock
from personaltrade.core.config import CostConfig, PaperConfig
from personaltrade.core.enums import Mode, OrderState, OrderType, Segment, Side
from personaltrade.core.logging import get_logger
from personaltrade.data.store.models import Instrument, Order, Position, Trade
from personaltrade.data.store.repos import (
    InstrumentRepository,
    OrderRepository,
    PaperAccountRepository,
    PositionRepository,
    TradeRepository,
)
from personaltrade.execution.broker import (
    BrokerPosition,
    Funds,
    OrderAck,
    OrderRequest,
    OrderStatus,
    OrderUpdate,
    QuoteSource,
    UnknownInstrument,
    UnknownOrder,
)

logger = get_logger(__name__)


class PaperBroker:
    def __init__(
        self,
        session: Session,
        quotes: QuoteSource,
        *,
        cost_rates: CostConfig,
        paper_cfg: PaperConfig,
        initial_cash: Decimal,
        clock: Clock | None = None,
    ) -> None:
        self.session = session
        self.quotes = quotes
        self.cost_rates = cost_rates
        self.segment = Segment(paper_cfg.segment)
        self.slippage_bps = paper_cfg.slippage_bps
        self.latency = timedelta(milliseconds=paper_cfg.latency_ms)
        self.clock = clock or SystemClock()
        self.orders = OrderRepository(session)
        self.positions = PositionRepository(session)
        self.trades = TradeRepository(session)
        self.instruments = InstrumentRepository(session)
        self.account = PaperAccountRepository(session).get_or_create(initial_cash)
        self._updates: deque[OrderUpdate] = deque()

    def place_order(self, order: OrderRequest) -> OrderAck:
        instrument = self.instruments.get(order.instrument_id)
        if instrument is None:
            raise UnknownInstrument(f"no instrument with id={order.instrument_id}")

        broker_order_id = f"PAPER-{uuid4()}"
        placed_at = self.clock.now()
        db_order = Order(
            client_order_id=order.client_order_id,
            broker_order_id=broker_order_id,
            instrument_id=order.instrument_id,
            side=order.side,
            order_type=order.order_type,
            qty=order.qty,
            limit_price=order.limit_price,
            mode=Mode.PAPER,
            state=OrderState.PENDING_RISK,
        )
        self.orders.record_created(db_order)
        self.orders.transition(db_order, OrderState.SUBMITTING)
        self.orders.transition(db_order, OrderState.SUBMITTED)
        self.orders.transition(db_order, OrderState.OPEN)
        self._updates.append(
            OrderUpdate(order.client_order_id, broker_order_id, OrderState.OPEN, 0, None, placed_at)
        )

        update = self._try_fill(db_order, instrument, placed_at)
        if update is not None:
            self._updates.append(update)

        return OrderAck(client_order_id=order.client_order_id, broker_order_id=broker_order_id)

    def cancel_order(self, client_order_id: str) -> None:
        db_order = self.orders.get_by_client_order_id(client_order_id)
        if db_order is None:
            raise UnknownOrder(f"no order with client_order_id={client_order_id!r}")
        if db_order.state not in (OrderState.OPEN, OrderState.PARTIALLY_FILLED):
            return  # already terminal — cancelling is idempotent, not an error
        self.orders.transition(db_order, OrderState.CANCELLED)
        self._updates.append(
            OrderUpdate(
                client_order_id,
                db_order.broker_order_id,
                OrderState.CANCELLED,
                db_order.filled_qty,
                None,
                self.clock.now(),
            )
        )

    def get_order_status(self, client_order_id: str) -> OrderStatus:
        db_order = self.orders.get_by_client_order_id(client_order_id)
        if db_order is None:
            raise UnknownOrder(f"no order with client_order_id={client_order_id!r}")
        return OrderStatus(
            client_order_id=db_order.client_order_id,
            broker_order_id=db_order.broker_order_id,
            state=db_order.state,
            qty=db_order.qty,
            filled_qty=db_order.filled_qty,
            avg_fill_price=self._avg_fill_price(db_order),
        )

    def get_positions(self) -> list[BrokerPosition]:
        return [
            BrokerPosition(instrument_id=p.instrument_id, qty=p.qty, avg_price=p.avg_price)
            for p in self.positions.list_open(Mode.PAPER)
        ]

    def get_funds(self) -> Funds:
        equity = self.account.cash
        for position in self.positions.list_open(Mode.PAPER):
            instrument = self.instruments.get(position.instrument_id)
            assert instrument is not None, "position references a deleted instrument"
            ltp = self.quotes.get_ltp(instrument) or position.avg_price
            equity += Decimal(position.qty) * ltp
        return Funds(cash=self.account.cash, equity=equity)

    async def stream_order_updates(self) -> AsyncIterator[OrderUpdate]:
        """Drains whatever's queued right now, then returns — there's no live loop
        yet to block on (M11's orchestrator). Safe to call repeatedly; each call
        only yields updates queued since the previous drain."""
        while self._updates:
            yield self._updates.popleft()

    def check_resting_orders(self) -> list[OrderUpdate]:
        """Re-attempt fills for every OPEN/PARTIALLY_FILLED order against the
        current quote. M9 has no live loop to call this automatically — M11's
        orchestrator calls it on each new quote/candle tick, the same way
        `place_order` calls `_try_fill` once immediately on submission."""
        collected: list[OrderUpdate] = []
        for db_order in self.orders.list_open(Mode.PAPER):
            instrument = self.instruments.get(db_order.instrument_id)
            assert instrument is not None, "order references a deleted instrument"
            update = self._try_fill(db_order, instrument, self.clock.now())
            if update is not None:
                self._updates.append(update)
                collected.append(update)
        return collected

    def _try_fill(
        self, db_order: Order, instrument: Instrument, at: datetime
    ) -> OrderUpdate | None:
        remaining = db_order.qty - db_order.filled_qty
        if remaining <= 0:
            return None

        ltp = self.quotes.get_ltp(instrument)
        if ltp is None:
            return None  # no quote available — stays OPEN, resting

        if db_order.order_type == OrderType.LIMIT:
            assert db_order.limit_price is not None, "LIMIT order must carry a limit_price"
            marketable = (
                ltp <= db_order.limit_price
                if db_order.side == Side.BUY
                else ltp >= db_order.limit_price
            )
            if not marketable:
                return None  # resting — retried by check_resting_orders() on a later quote
            # never worse than the limit for the trader
            reference_price = (
                min(ltp, db_order.limit_price)
                if db_order.side == Side.BUY
                else max(ltp, db_order.limit_price)
            )
        else:
            reference_price = ltp

        fill_price = apply_slippage(reference_price, db_order.side, self.slippage_bps)

        qty = remaining
        if db_order.side == Side.BUY:
            qty = self._clamp_to_cash(qty, fill_price)
        if qty <= 0:
            self.orders.transition(
                db_order, OrderState.CANCELLED, payload={"reason": "insufficient cash"}
            )
            return OrderUpdate(
                db_order.client_order_id,
                db_order.broker_order_id,
                OrderState.CANCELLED,
                db_order.filled_qty,
                None,
                at,
            )

        return self._execute_fill(db_order, instrument, qty, fill_price, at + self.latency)

    def _clamp_to_cash(self, qty: int, fill_price: Decimal) -> int:
        """Reduce qty until a BUY of that size (incl. costs) fits available cash —
        mirrors backtest/engine.py's `_clamp_to_cash` exactly (same reasoning: a
        strategy/sizer's requested qty is a request, not a guarantee)."""
        while qty > 0:
            if calculate_costs(
                Side.BUY, fill_price, qty, self.segment, self.cost_rates
            ).net_amount <= (self.account.cash):
                return qty
            qty -= 1
        return 0

    def _execute_fill(
        self, db_order: Order, instrument: Instrument, qty: int, fill_price: Decimal, at: datetime
    ) -> OrderUpdate:
        costs = calculate_costs(db_order.side, fill_price, qty, self.segment, self.cost_rates)
        position = self.positions.get_or_create(instrument.id, Mode.PAPER)
        realized_pnl = self._apply_fill_to_position(position, db_order.side, qty, costs.net_amount)

        self.trades.add(
            Trade(
                order_id=db_order.id,
                price=fill_price,
                qty=qty,
                brokerage=costs.brokerage,
                stt=costs.stt,
                stamp_duty=costs.stamp_duty,
                gst=costs.gst,
                exchange_charges=costs.exchange_charges,
                sebi_charges=costs.sebi_charges,
                net_amount=costs.net_amount,
                realized_pnl=realized_pnl,
                executed_at=at,
            )
        )

        db_order.filled_qty += qty
        new_state = (
            OrderState.FILLED
            if db_order.filled_qty >= db_order.qty
            else OrderState.PARTIALLY_FILLED
        )
        self.orders.transition(
            db_order, new_state, payload={"qty": qty, "fill_price": str(fill_price)}
        )

        logger.info(
            "paper_order_fill",
            client_order_id=db_order.client_order_id,
            side=str(db_order.side),
            qty=qty,
            fill_price=str(fill_price),
            state=str(new_state),
        )
        return OrderUpdate(
            db_order.client_order_id,
            db_order.broker_order_id,
            new_state,
            db_order.filled_qty,
            fill_price,
            at,
        )

    def _apply_fill_to_position(
        self, position: Position, side: Side, qty: int, net_amount: Decimal
    ) -> Decimal | None:
        """Mirrors backtest/engine.py's `_open_or_add`/`_close` math (ADR-015),
        adapted to a persisted Position row whose `realized_pnl` accumulates across
        the row's entire lifetime — `PositionRepository.get_or_create` is keyed by
        instrument+mode only, so the same row is reused across open/close cycles,
        unlike backtest's per-run `_Portfolio`. Relies on the invariant that a
        closing qty never exceeds the current position size, guaranteed by
        RiskEngine always sizing EXIT to exactly `abs(position.qty)` (ADR-018) —
        the only sanctioned path an OrderRequest reaches this broker from.

        Returns this leg's realized P&L (None for an opening/adding leg — only a
        closing leg realizes anything), for the caller to persist onto the Trade
        row (ROADMAP M11 needs per-trade realized P&L for daily-loss tracking).
        """
        cost_basis_per_share = net_amount / qty
        adding = position.qty == 0 or (position.qty > 0) == (side == Side.BUY)

        if adding:
            signed_qty = qty if side == Side.BUY else -qty
            if position.qty == 0:
                position.avg_price = cost_basis_per_share
            else:
                total_cost = position.avg_price * abs(position.qty) + cost_basis_per_share * qty
                position.avg_price = total_cost / (abs(position.qty) + qty)
            position.qty += signed_qty
            self.account.cash += -net_amount if side == Side.BUY else net_amount
            return None

        if side == Side.SELL:  # closing/reducing a long
            realized = net_amount - position.avg_price * qty
            self.account.cash += net_amount
            position.qty -= qty
        else:  # BUY, covering/reducing a short
            realized = position.avg_price * qty - net_amount
            self.account.cash -= net_amount
            position.qty += qty
        if position.qty == 0:
            position.avg_price = Decimal("0")
        position.realized_pnl += realized
        return realized

    def _avg_fill_price(self, db_order: Order) -> Decimal | None:
        trades = db_order.trades
        if not trades:
            return None
        total_qty = sum(t.qty for t in trades)
        total_value = Decimal("0")
        for t in trades:
            total_value += t.price * t.qty
        return total_value / Decimal(total_qty)
