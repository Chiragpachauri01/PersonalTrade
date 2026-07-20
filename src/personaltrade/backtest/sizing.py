"""Position sizing placeholder (ROADMAP M6; superseded by the real Risk Engine at M8).

Fixed-fractional: allocate risk_per_trade_pct of current equity to a new
position, at the current price. Deliberately simple — M8 replaces this with
ATR-based stop-distance sizing, exposure limits, and the kill switch,
without changing the Strategy or Backtester interfaces above it.
Cash-affordability clamping (accounting for costs) is the engine's job, not
the sizer's — see backtest/engine.py.
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
