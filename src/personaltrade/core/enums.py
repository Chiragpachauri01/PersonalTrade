"""Domain enums shared across modules. Values are stable — they are persisted to the DB."""

from __future__ import annotations

from enum import StrEnum


class Mode(StrEnum):
    PAPER = "PAPER"
    LIVE = "LIVE"
    BACKTEST = "BACKTEST"


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    SL = "SL"  # stop-loss


class OrderState(StrEnum):
    """See docs/architecture/04-trade-lifecycle.md for the state machine."""

    PENDING_RISK = "PENDING_RISK"
    REJECTED_RISK = "REJECTED_RISK"
    SUBMITTING = "SUBMITTING"
    SUBMITTED = "SUBMITTED"
    FAILED = "FAILED"
    REJECTED_BROKER = "REJECTED_BROKER"
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


#: Allowed order state transitions (docs/architecture/04-trade-lifecycle.md).
#: PARTIALLY_FILLED → PARTIALLY_FILLED covers successive partial fills.
ALLOWED_ORDER_TRANSITIONS: dict[OrderState, frozenset[OrderState]] = {
    OrderState.PENDING_RISK: frozenset({OrderState.REJECTED_RISK, OrderState.SUBMITTING}),
    OrderState.SUBMITTING: frozenset({OrderState.SUBMITTED, OrderState.FAILED}),
    OrderState.SUBMITTED: frozenset({OrderState.OPEN, OrderState.REJECTED_BROKER}),
    OrderState.OPEN: frozenset(
        {
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.CANCELLED,
            OrderState.EXPIRED,
        }
    ),
    OrderState.PARTIALLY_FILLED: frozenset(
        {
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.CANCELLED,
            OrderState.EXPIRED,
        }
    ),
    # terminal states
    OrderState.REJECTED_RISK: frozenset(),
    OrderState.FAILED: frozenset(),
    OrderState.REJECTED_BROKER: frozenset(),
    OrderState.FILLED: frozenset(),
    OrderState.CANCELLED: frozenset(),
    OrderState.EXPIRED: frozenset(),
}

TERMINAL_ORDER_STATES: frozenset[OrderState] = frozenset(
    state for state, targets in ALLOWED_ORDER_TRANSITIONS.items() if not targets
)


class Segment(StrEnum):
    """Which NSE equity segment a trade executes in — costs differ by segment."""

    DELIVERY = "DELIVERY"
    INTRADAY = "INTRADAY"


class Interval(StrEnum):
    """Candle intervals supported by the data pipeline."""

    M1 = "1m"
    M15 = "15m"
    D1 = "1d"


class SignalDirection(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"
    EXIT = "EXIT"


class SignalStatus(StrEnum):
    NEW = "NEW"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class RecommendationAction(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"
    AVOID = "AVOID"


class RiskEventKind(StrEnum):
    LIMIT_BREACH = "LIMIT_BREACH"
    KILL_SWITCH = "KILL_SWITCH"
    REJECTION = "REJECTION"
