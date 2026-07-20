"""EMA crossover entry with an ATR-based stop-loss exit.

LONG when the fast EMA crosses above the slow EMA. EXIT on either a
cross-down or the stop being hit, whichever comes first. Never shorts.

Stateful (docs/architecture/ADRS.md ADR-016): the stop level must be
anchored to the strategy's ACTUAL fill price, not the price at signal time —
a strategy only learns its real entry price one bar later, via
`ctx.position.avg_price` (which already reflects slippage and costs; see
backtest/engine.py). `self._stop` is therefore set lazily on the first bar
the strategy observes itself in a position, not when the LONG signal is
emitted. It is unconditionally cleared whenever the position is flat, which
also makes a strategy instance safe to reuse across a fresh (flat-starting)
run — though backtest/run.py additionally constructs a fresh instance per
symbol as defense in depth.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, Field

from personaltrade.core.enums import SignalDirection
from personaltrade.strategy.base import IndicatorSpec, Signal, StrategyContext


class EMAAtrStopParams(BaseModel):
    model_config = {"extra": "forbid"}

    fast_period: int = Field(default=12, ge=1)
    slow_period: int = Field(default=26, ge=2)
    atr_period: int = Field(default=14, ge=1)
    atr_multiplier: float = Field(default=2.5, gt=0)


class EMAAtrStopStrategy:
    """Trend-following: EMA crossover entry, ATR-stop or cross-down exit."""

    name: ClassVar[str] = "ema_atr_stop"
    params_schema: ClassVar[type[BaseModel]] = EMAAtrStopParams

    def __init__(self, params: EMAAtrStopParams | None = None) -> None:
        self.params = params or EMAAtrStopParams()
        if self.params.fast_period >= self.params.slow_period:
            raise ValueError("fast_period must be < slow_period")
        self._stop: float | None = None

    def clone(self) -> EMAAtrStopStrategy:
        return EMAAtrStopStrategy(self.params)

    def warmup_bars(self) -> int:
        return max(self.params.slow_period, self.params.atr_period) + 1

    def required_indicators(self) -> dict[str, IndicatorSpec]:
        return {
            "fast": IndicatorSpec("ema", {"period": self.params.fast_period}),
            "slow": IndicatorSpec("ema", {"period": self.params.slow_period}),
            "atr": IndicatorSpec("atr", {"period": self.params.atr_period}),
        }

    def on_candle(self, ctx: StrategyContext) -> Signal | None:
        fast = ctx.indicators.value("fast")
        slow = ctx.indicators.value("slow")
        atr = ctx.indicators.value("atr")
        fast_window = ctx.indicators.window("fast", 2)
        slow_window = ctx.indicators.window("slow", 2)
        if (
            fast is None
            or slow is None
            or atr is None
            or len(fast_window) < 2
            or len(slow_window) < 2
        ):
            return None

        prev_fast, prev_slow = fast_window[-2], slow_window[-2]
        close = float(ctx.candles["close"].iloc[-1])
        crossed_up = prev_fast <= prev_slow and fast > slow
        crossed_down = prev_fast >= prev_slow and fast < slow

        if ctx.position.is_flat:
            self._stop = None  # always clear: covers exit-by-stop and a fresh symbol's bar 0
            if crossed_up:
                return Signal(SignalDirection.LONG, close, {"fast": fast, "slow": slow, "atr": atr})
            return None

        # Long. First bar observing the fill -> anchor the stop to the real
        # entry price (ctx.position.avg_price), never the signal-time close.
        if self._stop is None:
            self._stop = ctx.position.avg_price - self.params.atr_multiplier * atr

        if close <= self._stop or crossed_down:
            reason = "stop" if close <= self._stop else "cross_down"
            self._stop = None
            return Signal(
                SignalDirection.EXIT, close, {"fast": fast, "slow": slow, "reason": reason}
            )
        return None
