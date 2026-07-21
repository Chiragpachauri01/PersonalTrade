"""Drives one Strategy against one live (instrument, interval) subscription —
the live analogue of backtest/engine.py's per-bar loop body (candle -> indicator
update -> warmup gate -> StrategyContext -> on_candle), minus fills (that's
RiskEngine + Broker, wired by orchestrator/service.py).
"""

from __future__ import annotations

from personaltrade.core.events import CandleReceived
from personaltrade.data.store.models import Instrument
from personaltrade.orchestrator.candle_buffer import LiveCandleBuffer
from personaltrade.orchestrator.indicator_bridge import LiveIndicatorView
from personaltrade.strategy.base import PositionView, Signal, Strategy, StrategyContext


class LiveStrategyRunner:
    def __init__(self, instrument: Instrument, strategy: Strategy) -> None:
        self.instrument = instrument
        self.strategy = strategy
        self.buffer = LiveCandleBuffer()
        self.indicators = LiveIndicatorView(strategy.required_indicators())
        self._bar_count = 0

    def on_candle(self, candle: CandleReceived, position: PositionView) -> Signal | None:
        """`position` is the CURRENT position, fetched by the caller
        (orchestrator/service.py) from the Position table immediately before
        this call — never cached here, since a fill from a prior bar's signal
        can change it between calls."""
        self.buffer.append(candle)
        self.indicators.update(
            high=float(candle.high), low=float(candle.low), close=float(candle.close)
        )
        self._bar_count += 1

        if self._bar_count < self.strategy.warmup_bars():
            return None
        if not self.indicators.all_warm():
            return None

        ctx = StrategyContext(
            index=self._bar_count - 1,
            ts=candle.ts,
            candles=self.buffer.frame(),
            indicators=self.indicators,
            position=position,
        )
        return self.strategy.on_candle(ctx)
