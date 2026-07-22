"""Startup reconciliation (docs/architecture/04-trade-lifecycle.md, CLAUDE.md
Rule 14: "safe to kill and restart at any point").

For paper mode there's no separate external broker state to diff against —
`PaperBroker`'s own tables already are the source of truth (ADR-019) — so this
narrows to one real check: an `Order` stuck in SUBMITTING/SUBMITTED. Today,
with the orchestrator wrapping one candle's whole signal-to-order flow in a
single committed transaction, a crash rolls that back entirely rather than
leaving a stuck row.

Live mode (ROADMAP M17, ADR-027) is where a real network round-trip can
genuinely succeed at the broker while the local commit still fails, and where
`Order`/`Position` can genuinely drift from the broker's own truth. This
function does two things when given an `UpstoxBroker`:

1. A stuck order **with** a `broker_order_id` (the ack was recorded locally
   before whatever interrupted the process) is resolved by asking Upstox for
   its real current status (`UpstoxBroker.poll_and_apply_fills()`) — not
   blindly marked FAILED, since the broker may well have filled it.
2. A stuck order **without** a `broker_order_id` is still marked FAILED,
   conservatively — Upstox may or may not have actually received it, but
   distinguishing the two needs a tag-based order-book search this module
   doesn't build yet (an honest, documented gap, not a silent guess); assuming
   failure and requiring a human to check the real account directly is the
   safe direction to be wrong in.
3. Position quantity mismatches are corrected broker-wins (docs/architecture/
   04-trade-lifecycle.md rule 5), logged as a `RiskEvent`, and trip the kill
   switch if the divergence exceeds `position_mismatch_kill_threshold_qty` —
   a mismatch that large means something is structurally wrong, not routine
   drift.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from personaltrade.core.enums import Mode, OrderState, RiskEventKind
from personaltrade.core.logging import get_logger
from personaltrade.data.store.models import Order, RiskEvent
from personaltrade.data.store.repos import OrderRepository, PositionRepository
from personaltrade.execution.upstox.broker import UpstoxBroker
from personaltrade.risk.kill_switch import KillSwitch

logger = get_logger(__name__)

_STUCK_STATES = (OrderState.SUBMITTING, OrderState.SUBMITTED)


@dataclass(frozen=True)
class ReconciliationFinding:
    client_order_id: str
    was_state: OrderState


def reconcile_on_startup(
    session: Session,
    mode: Mode,
    upstox_broker: UpstoxBroker | None = None,
    position_mismatch_kill_threshold_qty: int = 0,
) -> list[ReconciliationFinding]:
    orders = OrderRepository(session)
    stmt = select(Order).where(Order.mode == mode, Order.state.in_(_STUCK_STATES))
    if upstox_broker is not None:
        # A real broker exists to check with — only orders that never even
        # got an ack recorded are unconditionally FAILED here; ones with a
        # broker_order_id are resolved below via the real broker instead.
        stmt = stmt.where(Order.broker_order_id.is_(None))
    findings: list[ReconciliationFinding] = []
    for order in session.scalars(stmt).all():
        was_state = order.state
        orders.transition(
            order, OrderState.FAILED, payload={"reason": "found in non-terminal state at startup"}
        )
        session.add(
            RiskEvent(
                kind=RiskEventKind.REJECTION,
                detail={
                    "reason": "reconciliation_stuck_order",
                    "client_order_id": order.client_order_id,
                    "was_state": str(was_state),
                },
            )
        )
        findings.append(ReconciliationFinding(order.client_order_id, was_state))
        logger.warning(
            "reconciliation_stuck_order_failed",
            client_order_id=order.client_order_id,
            was_state=str(was_state),
        )

    if upstox_broker is not None:
        upstox_broker.poll_and_apply_fills()  # resolves stuck orders that DO have an ack
        _reconcile_positions(session, upstox_broker, position_mismatch_kill_threshold_qty)

    return findings


def _reconcile_positions(
    session: Session, upstox_broker: UpstoxBroker, kill_threshold_qty: int
) -> None:
    positions = PositionRepository(session)
    broker_by_instrument = {p.instrument_id: p for p in upstox_broker.get_positions()}
    local_by_instrument = {p.instrument_id: p for p in positions.list_open(Mode.LIVE)}

    for instrument_id in set(broker_by_instrument) | set(local_by_instrument):
        broker_pos = broker_by_instrument.get(instrument_id)
        local_pos = local_by_instrument.get(instrument_id)
        broker_qty = broker_pos.qty if broker_pos is not None else 0
        local_qty = local_pos.qty if local_pos is not None else 0
        if broker_qty == local_qty:
            continue

        divergence = abs(broker_qty - local_qty)
        if local_pos is None:
            local_pos = positions.get_or_create(instrument_id, Mode.LIVE)
        local_pos.qty = broker_qty
        if broker_pos is not None:
            local_pos.avg_price = broker_pos.avg_price
        elif broker_qty == 0:
            local_pos.avg_price = Decimal("0")

        breach = divergence > kill_threshold_qty
        session.add(
            RiskEvent(
                kind=RiskEventKind.LIMIT_BREACH if breach else RiskEventKind.REJECTION,
                detail={
                    "reason": "reconciliation_position_mismatch",
                    "instrument_id": instrument_id,
                    "local_qty_was": local_qty,
                    "broker_qty": broker_qty,
                },
            )
        )
        logger.warning(
            "reconciliation_position_corrected",
            instrument_id=instrument_id,
            local_qty_was=local_qty,
            broker_qty=broker_qty,
            divergence=divergence,
        )

        if breach:
            KillSwitch(session).trip(
                f"position reconciliation divergence {divergence} > threshold "
                f"{kill_threshold_qty} for instrument_id={instrument_id}"
            )
