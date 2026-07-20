"""Persisted kill switch (CLAUDE.md Rule 14, docs/architecture/04-trade-lifecycle.md).

Single source of truth for "is trading currently halted": the `kill_switch_state`
singleton row (`KillSwitchState`, id=1) — mirrors the Order/OrderEvent split already
used for order state elsewhere in this codebase (a mutable "what's true now" row,
alongside an append-only "what happened when" log), rather than re-deriving tripped
state from a scan of the RiskEvent history on every check. Every trip/reset still
appends a `RiskEvent` for that audit trail.

Trips from: max_consecutive_errors (`record_error`), or a caller-supplied reason
(limit breach, manual halt — `trip`). Reset always requires a human-supplied reason
(Rule 14: "one-command halt" implies a deliberate, logged un-halt too) and fails
loudly if the switch isn't currently tripped, so a reset is never silently a no-op.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from personaltrade.core.enums import RiskEventKind
from personaltrade.core.errors import PersonalTradeError
from personaltrade.data.store.models import KillSwitchState, RiskEvent
from personaltrade.data.store.repos import KillSwitchStateRepository
from personaltrade.data.store.types import utcnow


class KillSwitchNotTripped(PersonalTradeError):
    """Attempted to reset a kill switch that isn't currently tripped."""


class KillSwitch:
    def __init__(self, session: Session) -> None:
        self.session = session
        self._repo = KillSwitchStateRepository(session)

    def state(self) -> KillSwitchState:
        return self._repo.get_or_create()

    def is_tripped(self) -> bool:
        return self.state().tripped

    def trip(self, reason: str, detail: dict[str, Any] | None = None) -> KillSwitchState:
        """Idempotent: tripping an already-tripped switch logs nothing further —
        the first trip reason is the one that matters, repeats are just noise."""
        state = self.state()
        if state.tripped:
            return state
        state.tripped = True
        state.reason = reason
        state.tripped_at = utcnow()
        self.session.add(
            RiskEvent(kind=RiskEventKind.KILL_SWITCH, detail={"reason": reason, **(detail or {})})
        )
        self.session.flush()
        return state

    def reset(self, reason: str) -> KillSwitchState:
        state = self.state()
        if not state.tripped:
            raise KillSwitchNotTripped("kill switch is not tripped — nothing to reset")
        state.tripped = False
        state.reason = None
        state.tripped_at = None
        state.consecutive_errors = 0
        self.session.add(RiskEvent(kind=RiskEventKind.KILL_SWITCH_RESET, detail={"reason": reason}))
        self.session.flush()
        return state

    def record_error(self, max_consecutive_errors: int) -> KillSwitchState:
        """Call on every order-path failure (future orchestrator/broker calls, M9+).
        Auto-trips once the streak reaches the configured limit."""
        state = self.state()
        state.consecutive_errors += 1
        if state.consecutive_errors >= max_consecutive_errors and not state.tripped:
            return self.trip(
                reason=f"{state.consecutive_errors} consecutive errors "
                f"(limit {max_consecutive_errors})",
                detail={"consecutive_errors": state.consecutive_errors},
            )
        self.session.flush()
        return state

    def record_success(self) -> KillSwitchState:
        """Call on every order-path success — breaks an error streak before it trips."""
        state = self.state()
        if state.consecutive_errors != 0:
            state.consecutive_errors = 0
            self.session.flush()
        return state
