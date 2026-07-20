"""The Risk Engine (ROADMAP M8, docs/architecture/03-interfaces.md `RiskEngine`).

`evaluate(Signal) -> ApprovedOrder | Rejection` is the sole gate between a Signal and
an order — CLAUDE.md Rule 10 (LLM never touches the order path) and Rule 14
(kill switch) both terminate here. Every rejection is persisted to `risk_events`
(ROADMAP M8 deliverable); approvals are not (the resulting Order row, once the
orchestrator — M11 — creates one, is that audit trail; logging every approval too
would just duplicate it).

`equity` and `daily_realized_pnl` are caller-supplied, not derived internally —
see ADR-018. Nothing here can source them correctly yet (no live quotes until M10,
no Paper Broker fills until M9), and a placeholder computation would need to be
torn out and rebuilt the moment those milestones land, which is worse than an
honest, explicit input.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal
from enum import StrEnum
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from personaltrade.core.config import RiskConfig
from personaltrade.core.enums import Mode, OrderType, RiskEventKind, Side, SignalDirection
from personaltrade.data.store.models import Instrument, RiskEvent
from personaltrade.data.store.repos import PositionRepository, RiskEventRepository
from personaltrade.risk import limits
from personaltrade.risk.kill_switch import KillSwitch
from personaltrade.risk.sizing import PositionSizer
from personaltrade.strategy.base import Signal


class RejectionReason(StrEnum):
    KILL_SWITCH_TRIPPED = "KILL_SWITCH_TRIPPED"
    MAX_DAILY_LOSS = "MAX_DAILY_LOSS"
    MAX_OPEN_POSITIONS = "MAX_OPEN_POSITIONS"
    ALREADY_IN_POSITION = "ALREADY_IN_POSITION"
    NO_OPEN_POSITION = "NO_OPEN_POSITION"
    ZERO_QUANTITY = "ZERO_QUANTITY"


@dataclass(frozen=True)
class ApprovedOrder:
    """What the orchestrator (M11) is cleared to submit — sizing and the
    client_order_id (ADR-007 idempotency key) are decided here, not downstream."""

    client_order_id: str
    instrument_id: int
    side: Side
    order_type: OrderType
    qty: int
    limit_price: Decimal | None


@dataclass(frozen=True)
class Rejection:
    reason: RejectionReason
    detail: str


def _to_tick_decimal(price: float, tick_size: Decimal) -> Decimal:
    """Quantize a float (indicator-derived) price to the instrument's tick size —
    the risk/order boundary ADR-011 designates for float-analytics-to-Decimal-money
    conversion. Sizing only, not an order price (orders here are always MARKET)."""
    raw = Decimal(str(price))
    if tick_size <= 0:
        return raw
    ticks = (raw / tick_size).to_integral_value(rounding=ROUND_HALF_EVEN)
    return ticks * tick_size


class RiskEngine:
    def __init__(self, session: Session, config: RiskConfig, sizer: PositionSizer) -> None:
        self.session = session
        self.config = config
        self.sizer = sizer
        self.kill_switch = KillSwitch(session)
        self.positions = PositionRepository(session)
        self.risk_events = RiskEventRepository(session)

    def evaluate(
        self,
        signal: Signal,
        *,
        instrument: Instrument,
        mode: Mode,
        equity: Decimal,
        daily_realized_pnl: Decimal,
    ) -> ApprovedOrder | Rejection:
        if self.kill_switch.is_tripped():
            return self._reject(
                RejectionReason.KILL_SWITCH_TRIPPED,
                "kill switch is tripped",
                kind=RiskEventKind.REJECTION,
            )

        if limits.exceeds_max_daily_loss(
            daily_realized_pnl, equity, self.config.max_daily_loss_pct
        ):
            return self._reject(
                RejectionReason.MAX_DAILY_LOSS,
                f"today's realized P&L {daily_realized_pnl} exceeds "
                f"{self.config.max_daily_loss_pct}% of equity {equity}",
                kind=RiskEventKind.LIMIT_BREACH,
            )

        position = self.positions.get_for(instrument.id, mode)
        is_flat = position is None or position.qty == 0

        if signal.direction is SignalDirection.EXIT:
            if is_flat or position is None:
                return self._reject(
                    RejectionReason.NO_OPEN_POSITION,
                    "EXIT signal but no open position",
                    kind=RiskEventKind.REJECTION,
                )
            side = Side.SELL if position.qty > 0 else Side.BUY
            return self._approve(instrument, side, abs(position.qty))

        # LONG or SHORT: open a new position. No pyramiding, no same-bar reversal —
        # a strategy wanting to flip direction must emit EXIT first, then the new
        # direction later. Mirrors the backtest engine's fixed transition table
        # (ADR-015 point 3) so live/paper/backtest never disagree (ADR-006).
        if not is_flat and position is not None:
            return self._reject(
                RejectionReason.ALREADY_IN_POSITION,
                f"{signal.direction} signal but already in a position (qty={position.qty})",
                kind=RiskEventKind.REJECTION,
            )

        open_count = self.positions.count_open(mode)
        if limits.exceeds_max_open_positions(open_count, self.config.max_open_positions):
            return self._reject(
                RejectionReason.MAX_OPEN_POSITIONS,
                f"{open_count} open position(s) >= max {self.config.max_open_positions}",
                kind=RiskEventKind.LIMIT_BREACH,
            )

        price = _to_tick_decimal(signal.ref_price, instrument.tick_size)
        qty = self.sizer.size(equity, price)
        if qty <= 0:
            return self._reject(
                RejectionReason.ZERO_QUANTITY,
                f"sizer produced qty={qty} at equity={equity} price={price}",
                kind=RiskEventKind.REJECTION,
            )

        side = Side.BUY if signal.direction is SignalDirection.LONG else Side.SELL
        return self._approve(instrument, side, qty)

    def _approve(self, instrument: Instrument, side: Side, qty: int) -> ApprovedOrder:
        return ApprovedOrder(
            client_order_id=str(uuid4()),
            instrument_id=instrument.id,
            side=side,
            order_type=OrderType.MARKET,
            qty=qty,
            limit_price=None,
        )

    def _reject(self, reason: RejectionReason, detail: str, *, kind: RiskEventKind) -> Rejection:
        payload: dict[str, Any] = {"reason": reason.value, "detail": detail}
        self.risk_events.add(RiskEvent(kind=kind, detail=payload))
        return Rejection(reason=reason, detail=detail)
