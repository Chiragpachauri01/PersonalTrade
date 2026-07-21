"""Startup reconciliation (docs/architecture/04-trade-lifecycle.md, CLAUDE.md
Rule 14: "safe to kill and restart at any point").

For paper mode there's no separate external broker state to diff against —
`PaperBroker`'s own tables already are the source of truth (ADR-019) — so this
narrows to one real check: an `Order` stuck in SUBMITTING/SUBMITTED. Today,
with the orchestrator wrapping one candle's whole signal-to-order flow in a
single committed transaction, a crash rolls that back entirely rather than
leaving a stuck row — but a live broker (M17) involves a real network
round-trip that genuinely can succeed remotely while the local commit fails,
which is exactly this scenario. Kept here now: cheap, always-safe, and ready
for that day without needing to be added under pressure later. Live mode is
also where this function's full broker-vs-local diff (the numbered steps in
04-trade-lifecycle.md's Reconciliation section) has two independent sides to
actually reconcile.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from personaltrade.core.enums import Mode, OrderState, RiskEventKind
from personaltrade.core.logging import get_logger
from personaltrade.data.store.models import Order, RiskEvent
from personaltrade.data.store.repos import OrderRepository

logger = get_logger(__name__)

_STUCK_STATES = (OrderState.SUBMITTING, OrderState.SUBMITTED)


@dataclass(frozen=True)
class ReconciliationFinding:
    client_order_id: str
    was_state: OrderState


def reconcile_on_startup(session: Session, mode: Mode) -> list[ReconciliationFinding]:
    orders = OrderRepository(session)
    stmt = select(Order).where(Order.mode == mode, Order.state.in_(_STUCK_STATES))
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
    return findings
