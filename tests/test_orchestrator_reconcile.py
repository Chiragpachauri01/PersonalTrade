from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy.orm import Session

from personaltrade.core.config import CostConfig, UpstoxConfig
from personaltrade.core.enums import Mode, OrderState, OrderType, RiskEventKind, Side
from personaltrade.data.store.models import Instrument, Order, Position
from personaltrade.data.store.repos import (
    InstrumentRepository,
    PositionRepository,
    RiskEventRepository,
)
from personaltrade.execution.upstox.broker import UpstoxBroker
from personaltrade.orchestrator.reconcile import reconcile_on_startup
from personaltrade.risk.kill_switch import KillSwitch


def _instrument(session: Session) -> Instrument:
    inst = InstrumentRepository(session).add(
        Instrument(
            symbol="AAA", exchange="NSE", instrument_key="NSE_EQ|AAA", tick_size=Decimal("0.05")
        )
    )
    session.flush()
    return inst


def _order(
    session: Session, instrument: Instrument, state: OrderState, mode: Mode = Mode.PAPER
) -> Order:
    order = Order(
        client_order_id=f"co-{state.value}-{mode.value}",
        broker_order_id="PAPER-x",
        instrument_id=instrument.id,
        side=Side.BUY,
        order_type=OrderType.MARKET,
        qty=10,
        state=state,
        mode=mode,
    )
    session.add(order)
    session.flush()
    return order


class TestReconcileOnStartup:
    def test_no_stuck_orders_returns_empty(self, db_session: Session) -> None:
        instrument = _instrument(db_session)
        _order(db_session, instrument, OrderState.FILLED)
        assert reconcile_on_startup(db_session, Mode.PAPER) == []

    def test_submitting_order_marked_failed(self, db_session: Session) -> None:
        instrument = _instrument(db_session)
        order = _order(db_session, instrument, OrderState.SUBMITTING)
        findings = reconcile_on_startup(db_session, Mode.PAPER)
        assert len(findings) == 1
        assert findings[0].client_order_id == order.client_order_id
        assert findings[0].was_state == OrderState.SUBMITTING
        assert order.state == OrderState.FAILED

    def test_submitted_order_marked_failed(self, db_session: Session) -> None:
        instrument = _instrument(db_session)
        order = _order(db_session, instrument, OrderState.SUBMITTED)
        findings = reconcile_on_startup(db_session, Mode.PAPER)
        assert len(findings) == 1
        assert order.state == OrderState.FAILED

    def test_open_order_left_alone(self, db_session: Session) -> None:
        instrument = _instrument(db_session)
        order = _order(db_session, instrument, OrderState.OPEN)
        assert reconcile_on_startup(db_session, Mode.PAPER) == []
        assert order.state == OrderState.OPEN

    def test_partially_filled_order_left_alone(self, db_session: Session) -> None:
        instrument = _instrument(db_session)
        order = _order(db_session, instrument, OrderState.PARTIALLY_FILLED)
        assert reconcile_on_startup(db_session, Mode.PAPER) == []
        assert order.state == OrderState.PARTIALLY_FILLED

    def test_terminal_orders_left_alone(self, db_session: Session) -> None:
        instrument = _instrument(db_session)
        for state in (OrderState.FILLED, OrderState.CANCELLED, OrderState.FAILED):
            _order(db_session, instrument, state)
        assert reconcile_on_startup(db_session, Mode.PAPER) == []

    def test_only_matches_requested_mode(self, db_session: Session) -> None:
        instrument = _instrument(db_session)
        live_order = _order(db_session, instrument, OrderState.SUBMITTED, mode=Mode.LIVE)
        assert reconcile_on_startup(db_session, Mode.PAPER) == []
        assert live_order.state == OrderState.SUBMITTED

    def test_stuck_order_logs_a_risk_event(self, db_session: Session) -> None:
        instrument = _instrument(db_session)
        _order(db_session, instrument, OrderState.SUBMITTING)
        reconcile_on_startup(db_session, Mode.PAPER)
        events = RiskEventRepository(db_session).list_all()
        assert len(events) == 1
        assert events[0].kind == RiskEventKind.REJECTION
        assert events[0].detail["reason"] == "reconciliation_stuck_order"


def _upstox_broker(session: Session, handle: Any) -> UpstoxBroker:
    client = httpx.Client(transport=httpx.MockTransport(handle))
    return UpstoxBroker(
        session, client, "test-token", cfg=UpstoxConfig(), cost_rates=CostConfig()
    )


def _empty_positions_handle(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"status": "success", "data": []})


class TestReconcileLiveModeWithBroker:
    """ROADMAP M17, ADR-027: given a real (mocked) UpstoxBroker, reconciliation
    resolves acked-but-stuck orders via the broker instead of blindly failing
    them, and corrects position drift broker-wins."""

    def test_stuck_order_without_broker_order_id_still_marked_failed(
        self, db_session: Session
    ) -> None:
        instrument = _instrument(db_session)
        order = Order(
            client_order_id="co-no-ack",
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

        broker = _upstox_broker(db_session, _empty_positions_handle)
        findings = reconcile_on_startup(db_session, Mode.LIVE, broker)

        assert len(findings) == 1
        assert findings[0].client_order_id == "co-no-ack"
        assert order.state == OrderState.FAILED

    def test_stuck_order_with_broker_order_id_resolved_via_broker_not_failed(
        self, db_session: Session
    ) -> None:
        instrument = _instrument(db_session)
        order = Order(
            client_order_id="co-acked",
            broker_order_id="UP-999",
            instrument_id=instrument.id,
            side=Side.BUY,
            order_type=OrderType.MARKET,
            qty=10,
            state=OrderState.SUBMITTED,
            mode=Mode.LIVE,
        )
        db_session.add(order)
        db_session.flush()

        def handle(request: httpx.Request) -> httpx.Response:
            if "order/details" in str(request.url):
                return httpx.Response(
                    200,
                    json={
                        "status": "success",
                        "data": {"status": "complete", "quantity": 10, "filled_quantity": 10,
                                  "average_price": 1300.0},
                    },
                )
            return _empty_positions_handle(request)

        broker = _upstox_broker(db_session, handle)
        findings = reconcile_on_startup(db_session, Mode.LIVE, broker)

        assert findings == []  # not blindly failed
        assert order.state == OrderState.FILLED  # resolved via the real broker instead

    def test_position_mismatch_corrected_broker_wins(self, db_session: Session) -> None:
        instrument = _instrument(db_session)
        db_session.add(
            Position(instrument_id=instrument.id, qty=5, avg_price=Decimal("100"), mode=Mode.LIVE)
        )
        db_session.flush()

        def handle(request: httpx.Request) -> httpx.Response:
            if "portfolio" in str(request.url):
                return httpx.Response(
                    200,
                    json={
                        "status": "success",
                        "data": [
                            {
                                "instrument_token": instrument.instrument_key,
                                "quantity": 8,
                                "average_price": 102.5,
                                "last_price": 105.0,
                            }
                        ],
                    },
                )
            return httpx.Response(200, json={"status": "success", "data": []})

        broker = _upstox_broker(db_session, handle)
        reconcile_on_startup(db_session, Mode.LIVE, broker, position_mismatch_kill_threshold_qty=5)

        position = PositionRepository(db_session).get_for(instrument.id, Mode.LIVE)
        assert position is not None
        assert position.qty == 8
        assert position.avg_price == Decimal("102.5")
        assert not KillSwitch(db_session).is_tripped()  # divergence of 3 <= threshold of 5

    def test_position_mismatch_beyond_threshold_trips_kill_switch(
        self, db_session: Session
    ) -> None:
        instrument = _instrument(db_session)
        db_session.add(
            Position(instrument_id=instrument.id, qty=5, avg_price=Decimal("100"), mode=Mode.LIVE)
        )
        db_session.flush()

        def handle(request: httpx.Request) -> httpx.Response:
            if "portfolio" in str(request.url):
                return httpx.Response(
                    200,
                    json={
                        "status": "success",
                        "data": [
                            {
                                "instrument_token": instrument.instrument_key,
                                "quantity": 50,
                                "average_price": 102.5,
                                "last_price": 105.0,
                            }
                        ],
                    },
                )
            return httpx.Response(200, json={"status": "success", "data": []})

        broker = _upstox_broker(db_session, handle)
        reconcile_on_startup(db_session, Mode.LIVE, broker, position_mismatch_kill_threshold_qty=5)

        assert KillSwitch(db_session).is_tripped()
        events = RiskEventRepository(db_session).list_all()
        assert any(
            e.kind == RiskEventKind.LIMIT_BREACH
            and e.detail["reason"] == "reconciliation_position_mismatch"
            for e in events
        )

    def test_no_mismatch_no_broken_no_kill_switch(self, db_session: Session) -> None:
        instrument = _instrument(db_session)
        db_session.add(
            Position(instrument_id=instrument.id, qty=5, avg_price=Decimal("100"), mode=Mode.LIVE)
        )
        db_session.flush()

        def handle(request: httpx.Request) -> httpx.Response:
            if "portfolio" in str(request.url):
                return httpx.Response(
                    200,
                    json={
                        "status": "success",
                        "data": [
                            {
                                "instrument_token": instrument.instrument_key,
                                "quantity": 5,
                                "average_price": 100.0,
                                "last_price": 105.0,
                            }
                        ],
                    },
                )
            return httpx.Response(200, json={"status": "success", "data": []})

        broker = _upstox_broker(db_session, handle)
        reconcile_on_startup(db_session, Mode.LIVE, broker)

        assert not KillSwitch(db_session).is_tripped()
        assert RiskEventRepository(db_session).list_all() == []

    def test_position_only_at_broker_not_locally_is_created(self, db_session: Session) -> None:
        """A position that exists at the broker but has no local row at all
        (e.g. a fill applied entirely outside this process) is still caught
        and corrected — broker wins even when there's nothing local to diff
        against yet."""
        instrument = _instrument(db_session)

        def handle(request: httpx.Request) -> httpx.Response:
            if "portfolio" in str(request.url):
                return httpx.Response(
                    200,
                    json={
                        "status": "success",
                        "data": [
                            {
                                "instrument_token": instrument.instrument_key,
                                "quantity": 7,
                                "average_price": 200.0,
                                "last_price": 205.0,
                            }
                        ],
                    },
                )
            return httpx.Response(200, json={"status": "success", "data": []})

        broker = _upstox_broker(db_session, handle)
        reconcile_on_startup(db_session, Mode.LIVE, broker, position_mismatch_kill_threshold_qty=10)

        position = PositionRepository(db_session).get_for(instrument.id, Mode.LIVE)
        assert position is not None
        assert position.qty == 7
