"""Pure limit checks (ROADMAP M8) — no I/O, no session, table-driven testable in isolation
from RiskEngine's DB/kill-switch plumbing. Each function answers one yes/no question;
risk/engine.py sequences them and turns "yes" into a typed Rejection.
"""

from __future__ import annotations

from decimal import Decimal


def exceeds_max_open_positions(open_count: int, max_open_positions: int) -> bool:
    """True once a new position would push the count to or past the configured cap."""
    return open_count >= max_open_positions


def exceeds_max_daily_loss(
    daily_realized_pnl: Decimal, equity: Decimal, max_daily_loss_pct: Decimal
) -> bool:
    """True once today's realized loss reaches max_daily_loss_pct of equity.

    Only losses count — a profitable (or flat) day never trips this limit,
    regardless of how large the gain. `equity <= 0` can't express a percentage
    loss, so it's treated as "not exceeded" here; the sizer independently
    refuses to size anything against non-positive equity (ZERO_QUANTITY).
    """
    if daily_realized_pnl >= 0 or equity <= 0:
        return False
    loss_pct = (-daily_realized_pnl / equity) * Decimal(100)
    return loss_pct >= max_daily_loss_pct
