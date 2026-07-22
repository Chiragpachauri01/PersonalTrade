"""`UpstoxBroker` — the live `Broker` implementation (ROADMAP M17, ADR-027).

Read-only calls (get_funds/get_positions/get_order_status) are unconditional.
`place_order`/`cancel_order` have no dry-run branch of their own — the
two-key gate (ADR-008) lives in `risk/engine.py::RiskEngine.evaluate()`
(`LIVE_ORDERS_DISABLED`), so this class is simply never asked to place an
order while the gate is closed. When it IS called, it always genuinely calls
Upstox.

Wire contracts (URLs, fields, the order-status vocabulary) were verified
directly against Upstox's public API documentation (2026-07-22, see
ADR-027) rather than assumed from memory.

Fill costs are estimated with the same shared cost model the backtester and
Paper Broker use (`backtest/costs.py`, ADR-014) — Upstox's REST responses
don't include an itemized brokerage/STT/stamp-duty/GST breakdown, so this is
a documented estimate at the platform's configured rate card, not Upstox's
own contract note. Position qty/avg_price/realized_pnl are still updated
locally from these estimated fills (so risk/analytics have something to work
from between reconciliation passes), but `get_funds()`/`get_positions()`
always re-fetch from Upstox directly — there is no local cash ledger to
drift, unlike `PaperAccount` (ADR-019); reconciliation is the periodic
correcting force per docs/architecture/04-trade-lifecycle.md rule 5.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from datetime import datetime
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy.orm import Session

from personaltrade.backtest.costs import calculate_costs
from personaltrade.core.clock import Clock, SystemClock
from personaltrade.core.config import CostConfig, UpstoxConfig
from personaltrade.core.enums import Mode, OrderState, OrderType, Segment, Side
from personaltrade.core.errors import PersonalTradeError
from personaltrade.core.logging import get_logger
from personaltrade.data.providers.reconnect import ReconnectPolicy
from personaltrade.data.store.models import Order, Position, Trade
from personaltrade.data.store.repos import (
    InstrumentRepository,
    OrderRepository,
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
    UnknownInstrument,
    UnknownOrder,
)

logger = get_logger(__name__)

FUNDS_URL = "https://api.upstox.com/v2/user/get-funds-and-margin"
POSITIONS_URL = "https://api.upstox.com/v2/portfolio/short-term-positions"
ORDER_DETAILS_URL = "https://api.upstox.com/v2/order/details"
ORDER_BOOK_URL = "https://api.upstox.com/v2/order/retrieve-all"
#: Place/cancel use a dedicated low-latency host, distinct from every other
#: Upstox endpoint this codebase talks to (verified — not a typo).
PLACE_ORDER_URL = "https://api-hft.upstox.com/v2/order/place"
CANCEL_ORDER_URL = "https://api-hft.upstox.com/v2/order/cancel"

_SEGMENT_TO_PRODUCT = {Segment.DELIVERY: "D", Segment.INTRADAY: "I"}
_SIDE_TO_TRANSACTION_TYPE = {Side.BUY: "BUY", Side.SELL: "SELL"}
_ORDER_TYPE_TO_UPSTOX = {
    OrderType.MARKET: "MARKET",
    OrderType.LIMIT: "LIMIT",
    # No strategy in this codebase emits SL yet (risk/engine.py always sizes
    # MARKET); mapped defensively so the Protocol's full OrderType is at
    # least structurally handled, not silently wrong.
    OrderType.SL: "SL-M",
}

#: Upstox's Order Status appendix documents ~20 status strings (validation
#: pending, modify pending, cancel pending, ...); every one of them still
#: means "not yet a terminal outcome" from our coarser state machine's point
#: of view (docs/architecture/04-trade-lifecycle.md) except these three.
_TERMINAL_STATUS = {
    "complete": OrderState.FILLED,
    "rejected": OrderState.REJECTED_BROKER,
    "cancelled": OrderState.CANCELLED,
    "cancelled after market order": OrderState.CANCELLED,
}


class UpstoxBrokerError(PersonalTradeError):
    """Transport/API failure talking to Upstox — retryable, never a fabricated
    success (docs/architecture/03-interfaces.md `Broker` contract)."""


def _map_status(status: str, filled_qty: int, qty: int) -> OrderState:
    terminal = _TERMINAL_STATUS.get(status.lower())
    if terminal is not None:
        return terminal
    if filled_qty > 0:
        return OrderState.PARTIALLY_FILLED
    return OrderState.OPEN


class UpstoxBroker:
    def __init__(
        self,
        session: Session,
        client: httpx.Client,
        access_token: str,
        *,
        cfg: UpstoxConfig,
        cost_rates: CostConfig,
        clock: Clock | None = None,
    ) -> None:
        self.session = session
        self.client = client
        self.access_token = access_token
        self.cfg = cfg
        self.cost_rates = cost_rates
        self.segment = Segment(cfg.segment)
        self.clock = clock or SystemClock()
        self.orders = OrderRepository(session)
        self.positions = PositionRepository(session)
        self.trades = TradeRepository(session)
        self.instruments = InstrumentRepository(session)
        self._retry_policy = ReconnectPolicy(base_delay=0.5, max_delay=8.0)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}", "Accept": "application/json"}

    def _request(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        """One REST call with retry-with-backoff on 429/5xx (ROADMAP M17
        "rate-limit handling") — a 4xx other than 429 is a real request
        problem (bad params, rejected order) and is never retried."""
        attempt = 0
        while True:
            try:
                response = self.client.request(
                    method,
                    url,
                    headers=self._headers(),
                    timeout=self.cfg.request_timeout_seconds,
                    **kwargs,
                )
                response.raise_for_status()
                result: dict[str, Any] = response.json()
                return result
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                retryable = status == 429 or status >= 500
                if not retryable or attempt >= self.cfg.max_retries:
                    raise UpstoxBrokerError(
                        f"{method} {url} failed: HTTP {status}: {exc.response.text[:300]}"
                    ) from exc
                delay = self._retry_policy.delay_for(attempt)
                logger.warning(
                    "upstox_retrying", url=url, status=status, attempt=attempt, delay=delay
                )
                attempt += 1
                self._sleep(delay)
            except (httpx.HTTPError, ValueError) as exc:
                raise UpstoxBrokerError(f"{method} {url} failed: {exc}") from exc

    def _sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    # -- Broker Protocol -----------------------------------------------

    def place_order(self, order: OrderRequest) -> OrderAck:
        """Persists the Order row itself (PENDING_RISK -> SUBMITTING, before
        the network call — ADR-007 crash-safety invariant 1: "persist intent
        before action"), exactly mirroring `PaperBroker.place_order()` — the
        orchestrator (orchestrator/service.py::_process_candle) looks the row
        up by client_order_id immediately after this returns, expecting it to
        already exist.

        Known gap (documented in ADR-027, not silently glossed over): the
        orchestrator still wraps one candle's whole signal-to-order flow in a
        single transaction (ADR-021 decision 2). A crash between this
        method's SUBMITTING flush and Upstox's HTTP response would roll that
        row back with the rest of the transaction, exactly the scenario
        reconcile.py's own docstring already flagged as needing a real fix —
        deferred to a future milestone that splits the transaction boundary
        around a live network call, not solved here.
        """
        instrument = self.instruments.get(order.instrument_id)
        if instrument is None:
            raise UnknownInstrument(f"no instrument with id={order.instrument_id}")

        db_order = Order(
            client_order_id=order.client_order_id,
            instrument_id=order.instrument_id,
            side=order.side,
            order_type=order.order_type,
            qty=order.qty,
            limit_price=order.limit_price,
            mode=Mode.LIVE,
            state=OrderState.PENDING_RISK,
        )
        self.orders.record_created(db_order)
        self.orders.transition(db_order, OrderState.SUBMITTING)

        price = 0.0
        trigger_price = 0.0
        if order.order_type == OrderType.LIMIT and order.limit_price is not None:
            price = float(order.limit_price)
        elif order.order_type == OrderType.SL and order.limit_price is not None:
            trigger_price = float(order.limit_price)
        body = {
            "quantity": order.qty,
            "product": _SEGMENT_TO_PRODUCT[self.segment],
            "validity": "DAY",
            "price": price,
            "tag": order.client_order_id[:20],
            "instrument_token": instrument.instrument_key,
            "order_type": _ORDER_TYPE_TO_UPSTOX[order.order_type],
            "transaction_type": _SIDE_TO_TRANSACTION_TYPE[order.side],
            "disclosed_quantity": 0,
            "trigger_price": trigger_price,
            "is_amo": False,
            "market_protection": -1,
        }
        try:
            payload = self._request("POST", PLACE_ORDER_URL, json=body)
            broker_order_id = str(payload["data"]["order_id"])
        except (UpstoxBrokerError, KeyError, TypeError) as exc:
            self.orders.transition(
                db_order, OrderState.FAILED, payload={"reason": str(exc)}
            )
            raise UpstoxBrokerError(f"place_order failed: {exc}") from exc

        db_order.broker_order_id = broker_order_id
        self.orders.transition(db_order, OrderState.SUBMITTED)
        logger.info(
            "upstox_order_placed",
            client_order_id=order.client_order_id,
            broker_order_id=broker_order_id,
            side=str(order.side),
            qty=order.qty,
        )
        return OrderAck(client_order_id=order.client_order_id, broker_order_id=broker_order_id)

    def cancel_order(self, client_order_id: str) -> None:
        db_order = self.orders.get_by_client_order_id(client_order_id)
        if db_order is None or db_order.broker_order_id is None:
            raise UnknownOrder(f"no order with client_order_id={client_order_id!r}")
        if db_order.state not in (OrderState.OPEN, OrderState.PARTIALLY_FILLED):
            return  # already terminal — cancelling is idempotent, not an error
        self._request(
            "DELETE", CANCEL_ORDER_URL, params={"order_id": db_order.broker_order_id}
        )

    def get_order_status(self, client_order_id: str) -> OrderStatus:
        db_order = self.orders.get_by_client_order_id(client_order_id)
        if db_order is None or db_order.broker_order_id is None:
            raise UnknownOrder(f"no order with client_order_id={client_order_id!r}")
        payload = self._request(
            "GET", ORDER_DETAILS_URL, params={"order_id": db_order.broker_order_id}
        )
        data = payload.get("data", {})
        qty = int(data.get("quantity", db_order.qty))
        filled_qty = int(data.get("filled_quantity", 0))
        state = _map_status(str(data.get("status", "")), filled_qty, qty)
        avg_price = data.get("average_price")
        return OrderStatus(
            client_order_id=client_order_id,
            broker_order_id=db_order.broker_order_id,
            state=state,
            qty=qty,
            filled_qty=filled_qty,
            avg_fill_price=Decimal(str(avg_price)) if avg_price else None,
        )

    def _raw_positions(self) -> list[dict[str, Any]]:
        payload = self._request("GET", POSITIONS_URL)
        data = payload.get("data") or []
        return list(data)

    def get_positions(self) -> list[BrokerPosition]:
        result = []
        for row in self._raw_positions():
            instrument_token = str(row.get("instrument_token", ""))
            instrument = self.instruments.get_by_instrument_key(instrument_token)
            if instrument is None:
                logger.warning(
                    "upstox_position_unknown_instrument", instrument_token=instrument_token
                )
                continue
            qty = int(row.get("quantity", 0))
            if qty == 0:
                continue
            result.append(
                BrokerPosition(
                    instrument_id=instrument.id,
                    qty=qty,
                    avg_price=Decimal(str(row.get("average_price", "0"))),
                )
            )
        return result

    def get_funds(self) -> Funds:
        payload = self._request("GET", FUNDS_URL)
        equity_data = payload.get("data", {}).get("equity", {})
        cash = Decimal(str(equity_data.get("available_margin", "0")))
        mark_to_market = Decimal("0")
        for row in self._raw_positions():
            qty = int(row.get("quantity", 0))
            last_price = row.get("last_price")
            if qty and last_price is not None:
                mark_to_market += Decimal(qty) * Decimal(str(last_price))
        return Funds(cash=cash, equity=cash + mark_to_market)

    async def stream_order_updates(self) -> AsyncIterator[OrderUpdate]:
        """Not wired to a push feed yet — an honest, narrow gap (matching M10's
        own precedent for indicators with no live analogue): yields nothing.
        `poll_and_apply_fills()` below is what actually keeps local Order/
        Position state current for live trading, called by the orchestrator's
        housekeeping tick exactly like `PaperBroker.check_resting_orders()`.
        """
        return
        yield  # pragma: no cover - makes this a generator per the Protocol

    # -- Live-only housekeeping (not part of the Broker Protocol, mirrors
    #    PaperBroker.check_resting_orders()) ------------------------------

    def poll_and_apply_fills(self) -> list[OrderUpdate]:
        """For every locally OPEN/PARTIALLY_FILLED order, ask Upstox for its
        current status and apply any progress to local Order/Trade/Position
        state — the live analogue of `PaperBroker.check_resting_orders()`,
        except reading real fills instead of simulating them."""
        updates: list[OrderUpdate] = []
        for db_order in self.orders.list_open(Mode.LIVE):
            if db_order.broker_order_id is None:
                continue
            try:
                payload = self._request(
                    "GET", ORDER_DETAILS_URL, params={"order_id": db_order.broker_order_id}
                )
            except UpstoxBrokerError:
                logger.exception(
                    "upstox_poll_order_failed", client_order_id=db_order.client_order_id
                )
                continue
            update = self._apply_order_update(db_order, payload.get("data", {}))
            if update is not None:
                updates.append(update)
        return updates

    def _apply_order_update(self, db_order: Order, data: dict[str, Any]) -> OrderUpdate | None:
        qty = int(data.get("quantity", db_order.qty))
        filled_qty = int(data.get("filled_quantity", 0))
        status = str(data.get("status", ""))
        new_state = _map_status(status, filled_qty, qty)
        newly_filled = filled_qty - db_order.filled_qty

        if new_state == db_order.state and newly_filled <= 0:
            return None  # nothing changed since the last poll

        at = self.clock.now()
        avg_price = data.get("average_price")
        fill_price = Decimal(str(avg_price)) if avg_price else None

        if newly_filled > 0 and fill_price is not None:
            instrument = self.instruments.get(db_order.instrument_id)
            assert instrument is not None, "order references a deleted instrument"
            self._record_fill(db_order, instrument.id, newly_filled, fill_price, at)
            db_order.filled_qty = filled_qty

        if new_state != db_order.state:
            # SUBMITTED can only go directly to OPEN/REJECTED_BROKER/FAILED
            # (docs/architecture/04-trade-lifecycle.md's state diagram) — a
            # jump straight to FILLED/PARTIALLY_FILLED/CANCELLED must pass
            # through OPEN first, the "accepted by exchange" step.
            if db_order.state == OrderState.SUBMITTED and new_state not in (
                OrderState.OPEN,
                OrderState.REJECTED_BROKER,
                OrderState.FAILED,
            ):
                self.orders.transition(
                    db_order, OrderState.OPEN, payload={"upstox_status": status}
                )
            if new_state != db_order.state:
                self.orders.transition(db_order, new_state, payload={"upstox_status": status})

        logger.info(
            "upstox_order_update_applied",
            client_order_id=db_order.client_order_id,
            state=str(new_state),
            filled_qty=filled_qty,
        )
        return OrderUpdate(
            db_order.client_order_id,
            db_order.broker_order_id,
            new_state,
            filled_qty,
            fill_price,
            at,
        )

    def _record_fill(
        self, db_order: Order, instrument_id: int, qty: int, fill_price: Decimal, at: datetime
    ) -> None:
        """Estimated costs (module docstring) at this platform's configured
        rate card, applied to the real qty/price Upstox reports; Position
        qty/avg_price/realized_pnl are updated the same way
        `PaperBroker._apply_fill_to_position` does, minus any cash bookkeeping
        (there is no local cash ledger for a real broker — `get_funds()`
        always re-fetches from Upstox itself)."""
        costs = calculate_costs(db_order.side, fill_price, qty, self.segment, self.cost_rates)
        position = self.positions.get_or_create(instrument_id, Mode.LIVE)
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

    def _apply_fill_to_position(
        self, position: Position, side: Side, qty: int, net_amount: Decimal
    ) -> Decimal | None:
        """Mirrors `PaperBroker._apply_fill_to_position` exactly, minus the
        cash lines (see this method's caller's docstring)."""
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
            return None

        if side == Side.SELL:  # closing/reducing a long
            realized = net_amount - position.avg_price * qty
            position.qty -= qty
        else:  # BUY, covering/reducing a short
            realized = position.avg_price * qty - net_amount
            position.qty += qty
        if position.qty == 0:
            position.avg_price = Decimal("0")
        position.realized_pnl += realized
        return realized
