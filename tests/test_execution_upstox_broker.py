"""execution/upstox/broker.py: `UpstoxBroker` against Upstox's real,
verified wire contracts (ADR-027) via httpx.MockTransport — the established
mocking pattern (tests/test_provider_upstox.py).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy.orm import Session

from personaltrade.backtest.costs import calculate_costs
from personaltrade.core.config import CostConfig, UpstoxConfig
from personaltrade.core.enums import Mode, OrderState, OrderType, Segment, Side
from personaltrade.data.store.models import Instrument, Order
from personaltrade.data.store.repos import (
    InstrumentRepository,
    OrderRepository,
    PositionRepository,
)
from personaltrade.execution.broker import OrderRequest, UnknownInstrument, UnknownOrder
from personaltrade.execution.upstox.broker import UpstoxBroker, UpstoxBrokerError
from tests.factories import ManualClock


@pytest.fixture()
def instrument(db_session: Session) -> Instrument:
    inst = InstrumentRepository(db_session).add(
        Instrument(
            symbol="RELIANCE",
            exchange="NSE",
            instrument_key="NSE_EQ|INE002A01018",
            tick_size=Decimal("0.05"),
        )
    )
    db_session.flush()
    return inst


def _broker(
    session: Session, handle: Any, *, max_retries: int = 3, clock: ManualClock | None = None
) -> UpstoxBroker:
    client = httpx.Client(transport=httpx.MockTransport(handle))
    cfg = UpstoxConfig(max_retries=max_retries)
    broker = UpstoxBroker(
        session, client, "test-access-token", cfg=cfg, cost_rates=CostConfig(), clock=clock
    )
    broker._sleep = lambda seconds: None  # type: ignore[method-assign]  # keep retry tests fast
    return broker


def _order_request(instrument: Instrument, side: Side = Side.BUY, qty: int = 10) -> OrderRequest:
    return OrderRequest(
        client_order_id="co-1",
        instrument_id=instrument.id,
        side=side,
        order_type=OrderType.MARKET,
        qty=qty,
        limit_price=None,
    )


class TestPlaceOrder:
    def test_success_persists_order_and_returns_ack(
        self, db_session: Session, instrument: Instrument
    ) -> None:
        captured: dict[str, Any] = {}

        def handle(request: httpx.Request) -> httpx.Response:
            assert str(request.url) == "https://api-hft.upstox.com/v2/order/place"
            assert request.headers["Authorization"] == "Bearer test-access-token"
            import json

            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json={"status": "success", "data": {"order_id": "UP-123"}})

        broker = _broker(db_session, handle)
        ack = broker.place_order(_order_request(instrument))

        assert ack.client_order_id == "co-1"
        assert ack.broker_order_id == "UP-123"
        body = captured["body"]
        assert body["quantity"] == 10
        assert body["product"] == "D"
        assert body["transaction_type"] == "BUY"
        assert body["order_type"] == "MARKET"
        assert body["instrument_token"] == "NSE_EQ|INE002A01018"

        db_order = OrderRepository(db_session).get_by_client_order_id("co-1")
        assert db_order is not None
        assert db_order.state == OrderState.SUBMITTED
        assert db_order.broker_order_id == "UP-123"
        assert db_order.mode == Mode.LIVE

    def test_unknown_instrument_raises_before_any_request(self, db_session: Session) -> None:
        called = False

        def handle(request: httpx.Request) -> httpx.Response:
            nonlocal called
            called = True
            return httpx.Response(200, json={})

        broker = _broker(db_session, handle)
        request = OrderRequest(
            client_order_id="co-1",
            instrument_id=999,
            side=Side.BUY,
            order_type=OrderType.MARKET,
            qty=10,
            limit_price=None,
        )
        with pytest.raises(UnknownInstrument):
            broker.place_order(request)
        assert not called

    def test_api_failure_marks_order_failed_and_raises(
        self, db_session: Session, instrument: Instrument
    ) -> None:
        def handle(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"message": "insufficient funds"})

        broker = _broker(db_session, handle)
        with pytest.raises(UpstoxBrokerError):
            broker.place_order(_order_request(instrument))

        db_order = OrderRepository(db_session).get_by_client_order_id("co-1")
        assert db_order is not None
        assert db_order.state == OrderState.FAILED


class TestCancelOrder:
    def test_success_calls_cancel_with_broker_order_id(
        self, db_session: Session, instrument: Instrument
    ) -> None:
        captured: dict[str, Any] = {}

        def handle(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["method"] = request.method
            return httpx.Response(200, json={"status": "success", "data": {"order_id": "UP-1"}})

        order = Order(
            client_order_id="co-1",
            broker_order_id="UP-1",
            instrument_id=instrument.id,
            side=Side.BUY,
            order_type=OrderType.MARKET,
            qty=10,
            state=OrderState.OPEN,
            mode=Mode.LIVE,
        )
        db_session.add(order)
        db_session.flush()

        broker = _broker(db_session, handle)
        broker.cancel_order("co-1")

        assert captured["method"] == "DELETE"
        assert "order_id=UP-1" in captured["url"]

    def test_unknown_client_order_id_raises(self, db_session: Session) -> None:
        broker = _broker(db_session, lambda r: httpx.Response(200, json={}))
        with pytest.raises(UnknownOrder):
            broker.cancel_order("no-such-order")

    def test_terminal_order_is_a_no_op(self, db_session: Session, instrument: Instrument) -> None:
        called = False

        def handle(request: httpx.Request) -> httpx.Response:
            nonlocal called
            called = True
            return httpx.Response(200, json={})

        order = Order(
            client_order_id="co-1",
            broker_order_id="UP-1",
            instrument_id=instrument.id,
            side=Side.BUY,
            order_type=OrderType.MARKET,
            qty=10,
            state=OrderState.FILLED,
            mode=Mode.LIVE,
        )
        db_session.add(order)
        db_session.flush()

        broker = _broker(db_session, handle)
        broker.cancel_order("co-1")  # must not raise, must not call the API
        assert not called


class TestGetOrderStatus:
    def _stub_order(self, session: Session, instrument: Instrument) -> None:
        session.add(
            Order(
                client_order_id="co-1",
                broker_order_id="UP-1",
                instrument_id=instrument.id,
                side=Side.BUY,
                order_type=OrderType.MARKET,
                qty=10,
                state=OrderState.SUBMITTED,
                mode=Mode.LIVE,
            )
        )
        session.flush()

    @pytest.mark.parametrize(
        ("status", "filled", "qty", "expected_state"),
        [
            ("complete", 10, 10, OrderState.FILLED),
            ("rejected", 0, 10, OrderState.REJECTED_BROKER),
            ("cancelled", 0, 10, OrderState.CANCELLED),
            ("trigger pending", 0, 10, OrderState.OPEN),
            ("open", 4, 10, OrderState.PARTIALLY_FILLED),
            ("validation pending", 0, 10, OrderState.OPEN),
        ],
    )
    def test_maps_upstox_status_to_order_state(
        self,
        db_session: Session,
        instrument: Instrument,
        status: str,
        filled: int,
        qty: int,
        expected_state: OrderState,
    ) -> None:
        self._stub_order(db_session, instrument)

        def handle(request: httpx.Request) -> httpx.Response:
            assert "order_id=UP-1" in str(request.url)
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {
                        "status": status,
                        "quantity": qty,
                        "filled_quantity": filled,
                        "average_price": 1234.5 if filled else 0,
                    },
                },
            )

        broker = _broker(db_session, handle)
        result = broker.get_order_status("co-1")
        assert result.state == expected_state
        assert result.filled_qty == filled
        assert result.qty == qty

    def test_unknown_client_order_id_raises(self, db_session: Session) -> None:
        broker = _broker(db_session, lambda r: httpx.Response(200, json={}))
        with pytest.raises(UnknownOrder):
            broker.get_order_status("no-such-order")


class TestGetPositions:
    def test_maps_rows_to_broker_positions(
        self, db_session: Session, instrument: Instrument
    ) -> None:
        def handle(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": [
                        {
                            "instrument_token": "NSE_EQ|INE002A01018",
                            "quantity": 10,
                            "average_price": 1300.5,
                            "last_price": 1320.0,
                        }
                    ],
                },
            )

        broker = _broker(db_session, handle)
        positions = broker.get_positions()
        assert len(positions) == 1
        assert positions[0].instrument_id == instrument.id
        assert positions[0].qty == 10
        assert positions[0].avg_price == Decimal("1300.5")

    def test_skips_unknown_instrument_and_zero_qty(
        self, db_session: Session, instrument: Instrument
    ) -> None:
        def handle(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": [
                        {"instrument_token": "NSE_EQ|UNKNOWN", "quantity": 5, "average_price": 100},
                        {
                            "instrument_token": "NSE_EQ|INE002A01018",
                            "quantity": 0,
                            "average_price": 0,
                        },
                    ],
                },
            )

        broker = _broker(db_session, handle)
        assert broker.get_positions() == []


class TestGetFunds:
    def test_cash_and_equity_computed(self, db_session: Session, instrument: Instrument) -> None:
        def handle(request: httpx.Request) -> httpx.Response:
            if "funds-and-margin" in str(request.url):
                return httpx.Response(
                    200,
                    json={"status": "success", "data": {"equity": {"available_margin": 495000.0}}},
                )
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": [
                        {
                            "instrument_token": "NSE_EQ|INE002A01018",
                            "quantity": 10,
                            "average_price": 1300.0,
                            "last_price": 1320.0,
                        }
                    ],
                },
            )

        broker = _broker(db_session, handle)
        funds = broker.get_funds()
        assert funds.cash == Decimal("495000.0")
        assert funds.equity == Decimal("495000.0") + Decimal("10") * Decimal("1320.0")


class TestPollAndApplyFills:
    def test_new_fill_creates_trade_and_updates_position(
        self, db_session: Session, instrument: Instrument
    ) -> None:
        order = Order(
            client_order_id="co-1",
            broker_order_id="UP-1",
            instrument_id=instrument.id,
            side=Side.BUY,
            order_type=OrderType.MARKET,
            qty=10,
            filled_qty=0,
            state=OrderState.SUBMITTED,
            mode=Mode.LIVE,
        )
        db_session.add(order)
        db_session.flush()

        def handle(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {
                        "status": "complete",
                        "quantity": 10,
                        "filled_quantity": 10,
                        "average_price": 1300.0,
                    },
                },
            )

        clock = ManualClock()
        broker = _broker(db_session, handle, clock=clock)
        updates = broker.poll_and_apply_fills()

        assert len(updates) == 1
        assert updates[0].to_state == OrderState.FILLED
        assert order.state == OrderState.FILLED
        assert order.filled_qty == 10

        position = PositionRepository(db_session).get_for(instrument.id, Mode.LIVE)
        assert position is not None
        assert position.qty == 10
        # avg_price folds in this leg's own estimated transaction costs
        # (ADR-015 point 4, reused here per this module's own docstring) —
        # not the bare fill price.
        expected_costs = calculate_costs(
            Side.BUY, Decimal("1300.0"), 10, Segment.DELIVERY, CostConfig()
        )
        assert position.avg_price == expected_costs.net_amount / 10

        trades = list(order.trades)
        assert len(trades) == 1
        assert trades[0].qty == 10
        assert trades[0].price == Decimal("1300.0")

    def test_no_change_yields_nothing(self, db_session: Session, instrument: Instrument) -> None:
        order = Order(
            client_order_id="co-1",
            broker_order_id="UP-1",
            instrument_id=instrument.id,
            side=Side.BUY,
            order_type=OrderType.MARKET,
            qty=10,
            filled_qty=0,
            state=OrderState.OPEN,  # already resolved past SUBMITTED — genuinely nothing new
            mode=Mode.LIVE,
        )
        db_session.add(order)
        db_session.flush()

        def handle(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {
                        "status": "open",
                        "quantity": 10,
                        "filled_quantity": 0,
                    },
                },
            )

        broker = _broker(db_session, handle)
        assert broker.poll_and_apply_fills() == []
        assert order.state == OrderState.OPEN

    def test_ignores_orders_with_no_broker_order_id(
        self, db_session: Session, instrument: Instrument
    ) -> None:
        order = Order(
            client_order_id="co-1",
            broker_order_id=None,
            instrument_id=instrument.id,
            side=Side.BUY,
            order_type=OrderType.MARKET,
            qty=10,
            state=OrderState.SUBMITTING,
            mode=Mode.LIVE,
        )
        db_session.add(order)
        db_session.flush()

        called = False

        def handle(request: httpx.Request) -> httpx.Response:
            nonlocal called
            called = True
            return httpx.Response(200, json={})

        broker = _broker(db_session, handle)
        assert broker.poll_and_apply_fills() == []
        assert not called


class TestRetryBehavior:
    def _stub_order(self, session: Session, instrument: Instrument) -> None:
        session.add(
            Order(
                client_order_id="co-1",
                broker_order_id="UP-1",
                instrument_id=instrument.id,
                side=Side.BUY,
                order_type=OrderType.MARKET,
                qty=10,
                state=OrderState.SUBMITTED,
                mode=Mode.LIVE,
            )
        )
        session.flush()

    def test_retries_on_429_then_succeeds(
        self, db_session: Session, instrument: Instrument
    ) -> None:
        self._stub_order(db_session, instrument)
        attempts = {"count": 0}

        def handle(request: httpx.Request) -> httpx.Response:
            attempts["count"] += 1
            if attempts["count"] < 3:
                return httpx.Response(429, json={"message": "rate limited"})
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {"status": "open", "quantity": 10, "filled_quantity": 0},
                },
            )

        broker = _broker(db_session, handle, max_retries=5)
        status = broker.get_order_status("co-1")
        assert status.state == OrderState.OPEN
        assert attempts["count"] == 3

    def test_gives_up_after_max_retries(self, db_session: Session, instrument: Instrument) -> None:
        self._stub_order(db_session, instrument)

        def handle(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, json={"message": "unavailable"})

        broker = _broker(db_session, handle, max_retries=2)
        with pytest.raises(UpstoxBrokerError):
            broker.get_order_status("co-1")

    def test_non_retryable_4xx_raises_immediately(
        self, db_session: Session, instrument: Instrument
    ) -> None:
        self._stub_order(db_session, instrument)
        attempts = {"count": 0}

        def handle(request: httpx.Request) -> httpx.Response:
            attempts["count"] += 1
            return httpx.Response(404, json={"message": "not found"})

        broker = _broker(db_session, handle, max_retries=5)
        with pytest.raises(UpstoxBrokerError):
            broker.get_order_status("co-1")
        assert attempts["count"] == 1
