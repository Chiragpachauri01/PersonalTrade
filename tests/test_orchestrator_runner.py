"""LiveStrategyRunner (ROADMAP M11): warmup gating (bar count AND indicator
all_warm, mirroring ADR-015's backtest rule), StrategyContext construction,
and hand-off to Strategy.on_candle().
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import ClassVar

from pydantic import BaseModel

from personaltrade.core.enums import Interval, SignalDirection
from personaltrade.core.events import CandleReceived
from personaltrade.data.store.models import Instrument
from personaltrade.orchestrator.runner import LiveStrategyRunner
from personaltrade.strategy.base import (
    FLAT_POSITION,
    IndicatorSpec,
    PositionView,
    Signal,
    StrategyContext,
)


class _Params(BaseModel):
    model_config = {"extra": "forbid"}


class _RecordingStrategy:
    """Requires one SMA(2) indicator, warmup_bars=1 (deliberately LESS than the
    indicator's own warm-up, so bar-count and indicator gating are tested
    independently), and records every StrategyContext it's given."""

    name: ClassVar[str] = "recording"
    params_schema: ClassVar[type[BaseModel]] = _Params

    def __init__(self, params: _Params | None = None) -> None:
        self.params = params or _Params()
        self.contexts: list[StrategyContext] = []

    def clone(self) -> _RecordingStrategy:
        return _RecordingStrategy(self.params)

    def warmup_bars(self) -> int:
        return 1

    def required_indicators(self) -> dict[str, IndicatorSpec]:
        return {"fast": IndicatorSpec("sma", {"period": 2})}

    def on_candle(self, ctx: StrategyContext) -> Signal | None:
        self.contexts.append(ctx)
        return Signal(SignalDirection.LONG, ref_price=float(ctx.candles["close"].iloc[-1]))


def _instrument() -> Instrument:
    inst = Instrument(
        symbol="X", exchange="NSE", instrument_key="NSE_EQ|X", tick_size=Decimal("0.05")
    )
    inst.id = 1
    return inst


def _candle(ts: datetime, close: str) -> CandleReceived:
    return CandleReceived(
        instrument_key="NSE_EQ|X",
        interval=Interval.M1,
        ts=ts,
        open=Decimal(close),
        high=Decimal(close),
        low=Decimal(close),
        close=Decimal(close),
        volume=10,
    )


class TestWarmupGating:
    def test_no_signal_before_indicator_is_warm_even_if_bar_count_satisfied(self) -> None:
        strategy = _RecordingStrategy()
        runner = LiveStrategyRunner(_instrument(), strategy)
        # warmup_bars()=1 is satisfied on the very first candle, but SMA(2)
        # needs a second tick before it produces a value.
        signal = runner.on_candle(
            _candle(datetime(2026, 1, 1, 9, 15, tzinfo=UTC), "100"), FLAT_POSITION
        )
        assert signal is None
        assert strategy.contexts == []

    def test_signal_once_both_bar_count_and_indicator_are_ready(self) -> None:
        strategy = _RecordingStrategy()
        runner = LiveStrategyRunner(_instrument(), strategy)
        runner.on_candle(_candle(datetime(2026, 1, 1, 9, 15, tzinfo=UTC), "100"), FLAT_POSITION)
        signal = runner.on_candle(
            _candle(datetime(2026, 1, 1, 9, 16, tzinfo=UTC), "102"), FLAT_POSITION
        )
        assert signal is not None
        assert signal.direction == SignalDirection.LONG
        assert len(strategy.contexts) == 1


class TestStrategyContextConstruction:
    def test_context_carries_candles_indicators_and_position(self) -> None:
        strategy = _RecordingStrategy()
        runner = LiveStrategyRunner(_instrument(), strategy)
        position = PositionView(qty=10, avg_price=101.5)
        runner.on_candle(_candle(datetime(2026, 1, 1, 9, 15, tzinfo=UTC), "100"), FLAT_POSITION)
        runner.on_candle(_candle(datetime(2026, 1, 1, 9, 16, tzinfo=UTC), "102"), position)

        ctx = strategy.contexts[0]
        assert ctx.index == 1
        assert ctx.ts == datetime(2026, 1, 1, 9, 16, tzinfo=UTC)
        assert list(ctx.candles["close"]) == [100.0, 102.0]
        assert ctx.indicators.value("fast") == 101.0
        assert ctx.position == position
