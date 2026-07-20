"""Position sizing (ROADMAP M8; moved here from backtest/sizing.py, its M6 placeholder
location — ADR-015 called this move out in advance so the backtester and the live
Risk Engine would size positions with the exact same code, never two implementations
that could silently drift, per CLAUDE.md Rule 11).

Fixed-fractional: allocate risk_per_trade_pct of current equity to a new
position, at the current price. Cash-affordability clamping (accounting for
costs) is each caller's job (backtest/engine.py; risk/engine.py), not the
sizer's.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol


class PositionSizer(Protocol):
    def size(self, equity: Decimal, price: Decimal) -> int:
        """Shares to buy/sell to open a position, before cash/cost clamping. May be 0."""
        ...


class FixedFractionalSizer:
    """qty = floor(equity * risk_pct / price)."""

    def __init__(self, risk_per_trade_pct: Decimal) -> None:
        if not (Decimal(0) < risk_per_trade_pct <= Decimal(100)):
            raise ValueError(f"risk_per_trade_pct must be in (0, 100], got {risk_per_trade_pct}")
        self.risk_per_trade_pct = risk_per_trade_pct

    def size(self, equity: Decimal, price: Decimal) -> int:
        if price <= 0 or equity <= 0:
            return 0
        allocation = equity * (self.risk_per_trade_pct / Decimal(100))
        return max(int(allocation // price), 0)
