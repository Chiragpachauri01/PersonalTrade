"""SMA crossover: LONG when fast SMA crosses above slow SMA, EXIT on cross-down.

The simplest reference strategy — stateless, long-only. Originally shipped
in M6 as backtest-engine smoke-test plumbing (personaltrade.strategy.examples);
moved here now that the registry (M7) gives it a permanent, discoverable home.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, Field

from personaltrade.core.enums import SignalDirection
from personaltrade.strategy.base import IndicatorSpec, Signal, StrategyContext


class SMACrossoverParams(BaseModel):
    model_config = {"extra": "forbid"}

    fast_period: int = Field(default=10, ge=1)
    slow_period: int = Field(default=30, ge=2)


class SMACrossoverStrategy:
    """Golden/death cross. Never shorts."""

    name: ClassVar[str] = "sma_crossover"
    params_schema: ClassVar[type[BaseModel]] = SMACrossoverParams

    def __init__(self, params: SMACrossoverParams | None = None) -> None:
        self.params = params or SMACrossoverParams()
        if self.params.fast_period >= self.params.slow_period:
            raise ValueError("fast_period must be < slow_period")

    def clone(self) -> SMACrossoverStrategy:
        return SMACrossoverStrategy(self.params)

    def warmup_bars(self) -> int:
        return self.params.slow_period + 1  # +1 so both bars of the crossover check are valid

    def required_indicators(self) -> dict[str, IndicatorSpec]:
        return {
            "fast": IndicatorSpec("sma", {"period": self.params.fast_period}),
            "slow": IndicatorSpec("sma", {"period": self.params.slow_period}),
        }

    def on_candle(self, ctx: StrategyContext) -> Signal | None:
        fast = ctx.indicators.value("fast")
        slow = ctx.indicators.value("slow")
        fast_window = ctx.indicators.window("fast", 2)
        slow_window = ctx.indicators.window("slow", 2)
        if fast is None or slow is None or len(fast_window) < 2 or len(slow_window) < 2:
            return None

        prev_fast, prev_slow = fast_window[-2], slow_window[-2]
        close = float(ctx.candles["close"].iloc[-1])

        crossed_up = prev_fast <= prev_slow and fast > slow
        crossed_down = prev_fast >= prev_slow and fast < slow

        if crossed_up and ctx.position.is_flat:
            return Signal(SignalDirection.LONG, close, {"fast": fast, "slow": slow})
        if crossed_down and ctx.position.is_long:
            return Signal(SignalDirection.EXIT, close, {"fast": fast, "slow": slow})
        return None
