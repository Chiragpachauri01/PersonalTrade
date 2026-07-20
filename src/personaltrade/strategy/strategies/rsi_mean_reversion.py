"""RSI mean reversion: fade oversold extremes, exit as RSI reverts toward the middle.

LONG when RSI crosses below `oversold` (betting on a bounce). EXIT when RSI
crosses back above `exit_level`. Never shorts. Fully stateless — unlike
ema_atr_stop, there is no per-position memory to manage or leak across
symbols.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, Field, model_validator

from personaltrade.core.enums import SignalDirection
from personaltrade.strategy.base import IndicatorSpec, Signal, StrategyContext


class RSIMeanReversionParams(BaseModel):
    model_config = {"extra": "forbid"}

    rsi_period: int = Field(default=14, ge=2)
    oversold: float = Field(default=30.0, gt=0, lt=100)
    exit_level: float = Field(default=50.0, gt=0, lt=100)

    @model_validator(mode="after")
    def _oversold_below_exit(self) -> RSIMeanReversionParams:
        if not self.oversold < self.exit_level:
            raise ValueError("oversold must be < exit_level")
        return self


class RSIMeanReversionStrategy:
    """Counter-trend: fade RSI oversold, exit on reversion toward the midline."""

    name: ClassVar[str] = "rsi_mean_reversion"
    params_schema: ClassVar[type[BaseModel]] = RSIMeanReversionParams

    def __init__(self, params: RSIMeanReversionParams | None = None) -> None:
        self.params = params or RSIMeanReversionParams()

    def clone(self) -> RSIMeanReversionStrategy:
        return RSIMeanReversionStrategy(self.params)

    def warmup_bars(self) -> int:
        return (
            self.params.rsi_period + 2
        )  # RSI needs period+1 bars, plus one more for the crossing check

    def required_indicators(self) -> dict[str, IndicatorSpec]:
        return {"rsi": IndicatorSpec("rsi", {"period": self.params.rsi_period})}

    def on_candle(self, ctx: StrategyContext) -> Signal | None:
        rsi = ctx.indicators.value("rsi")
        window = ctx.indicators.window("rsi", 2)
        if rsi is None or len(window) < 2:
            return None

        prev_rsi = window[-2]
        close = float(ctx.candles["close"].iloc[-1])

        crossed_below_oversold = prev_rsi >= self.params.oversold and rsi < self.params.oversold
        crossed_above_exit = prev_rsi <= self.params.exit_level and rsi > self.params.exit_level

        if crossed_below_oversold and ctx.position.is_flat:
            return Signal(SignalDirection.LONG, close, {"rsi": rsi})
        if crossed_above_exit and ctx.position.is_long:
            return Signal(SignalDirection.EXIT, close, {"rsi": rsi})
        return None
