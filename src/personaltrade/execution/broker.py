"""The `Broker` contract (docs/architecture/03-interfaces.md) and its DTOs.

Every field here mirrors an existing `Order`/`Trade`/`Position` ORM column 1:1 where one
exists (docs/architecture/02-data-model.md) — these are transport-shaped views over that
state, not a parallel model. Implementations never raise on business rejections (a
cancel on an already-terminal order, a limit order that never crosses) — only on
transport-shaped failures (unknown client_order_id) or a truly unmet precondition.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from personaltrade.core.enums import OrderState, OrderType, Side
from personaltrade.core.errors import PersonalTradeError
from personaltrade.data.store.models import Instrument


class UnknownOrder(PersonalTradeError):
    """No order with the given client_order_id exists."""


class UnknownInstrument(PersonalTradeError):
    """No instrument with the given id exists — an OrderRequest referencing one
    that doesn't (or no longer) exist is a caller bug, not a business rejection."""


@dataclass(frozen=True)
class OrderRequest:
    """What the orchestrator (M11) submits — the shape of `risk.engine.ApprovedOrder`,
    duplicated here rather than imported so `execution` never depends on `risk`
    (Rule 7: replaceable, one-directional module dependencies)."""

    client_order_id: str
    instrument_id: int
    side: Side
    order_type: OrderType
    qty: int
    limit_price: Decimal | None


@dataclass(frozen=True)
class OrderAck:
    client_order_id: str
    broker_order_id: str


@dataclass(frozen=True)
class OrderStatus:
    client_order_id: str
    broker_order_id: str | None
    state: OrderState
    qty: int
    filled_qty: int
    avg_fill_price: Decimal | None


@dataclass(frozen=True)
class BrokerPosition:
    instrument_id: int
    qty: int  # signed: >0 long, <0 short
    avg_price: Decimal


@dataclass(frozen=True)
class Funds:
    cash: Decimal
    equity: Decimal  # cash + mark-to-market value of all open positions


@dataclass(frozen=True)
class OrderUpdate:
    """One state transition, for `stream_order_updates` — deliberately narrower than
    the full `OrderStatus` (a diff, not a snapshot), matching what an event-driven
    consumer (M11's orchestrator) actually needs per update."""

    client_order_id: str
    broker_order_id: str | None
    to_state: OrderState
    filled_qty: int
    fill_price: Decimal | None
    at: datetime


class QuoteSource(Protocol):
    """The Paper Broker's only view of "the market" — deliberately narrower than
    `MarketDataProvider` (M4/M10): a single last-traded-price lookup, nothing
    async/streaming. `execution/paper/quotes.py::ReplayQuoteSource` (last synced
    candle close) is the only implementation until M10 ships a live one behind
    this same Protocol — the Paper Broker itself never changes."""

    def get_ltp(self, instrument: Instrument) -> Decimal | None:
        """Last traded price, or None if no quote is available for this instrument."""
        ...


class Broker(Protocol):
    """Paper (M9) and Upstox (M17) implement this identically. Selected by config
    `trading.broker`. The orchestrator (M11) is the only caller, and only with a
    RiskEngine-approved order (Rule 10, 14)."""

    def place_order(self, order: OrderRequest) -> OrderAck: ...
    def cancel_order(self, client_order_id: str) -> None: ...
    def get_order_status(self, client_order_id: str) -> OrderStatus: ...
    def get_positions(self) -> list[BrokerPosition]: ...
    def get_funds(self) -> Funds: ...
    def stream_order_updates(self) -> AsyncIterator[OrderUpdate]: ...
