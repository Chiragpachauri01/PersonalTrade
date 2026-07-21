from __future__ import annotations

from decimal import Decimal

from sqlalchemy.orm import Session

from personaltrade.core.enums import Mode, OrderState, OrderType, RiskEventKind, Side
from personaltrade.data.store.models import Instrument, Order
from personaltrade.data.store.repos import InstrumentRepository, RiskEventRepository
from personaltrade.orchestrator.reconcile import reconcile_on_startup


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
