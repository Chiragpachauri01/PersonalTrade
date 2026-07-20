"""RSIMeanReversionStrategy: stateless, so no cross-symbol leak concern —
tests focus on the crossing logic and the deterministic-replay guarantee."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pandas as pd
import pytest

from personaltrade.backtest.engine import run_backtest
from personaltrade.backtest.indicator_bridge import BatchIndicatorView
from personaltrade.core.config import CostConfig
from personaltrade.core.enums import SignalDirection
from personaltrade.risk.sizing import FixedFractionalSizer
from personaltrade.strategy.base import FLAT_POSITION, PositionView, StrategyContext
from personaltrade.strategy.strategies.rsi_mean_reversion import (
    RSIMeanReversionParams,
    RSIMeanReversionStrategy,
)
from tests.factories import synthetic_candles


def _ctx(rsi_values: list[float], position: PositionView) -> StrategyContext:
    n = len(rsi_values)
    return StrategyContext(
        index=n - 1,
        ts=datetime(2026, 1, 1, tzinfo=UTC),
        candles=pd.DataFrame({"close": rsi_values}),
        indicators=BatchIndicatorView({"rsi": pd.Series(rsi_values)}, n - 1),
        position=position,
    )


class TestConstructionAndDeclaration:
    def test_rejects_oversold_not_below_exit_level(self) -> None:
        with pytest.raises(ValueError, match="oversold must be"):
            RSIMeanReversionParams(oversold=60.0, exit_level=50.0)

    def test_warmup_is_period_plus_two(self) -> None:
        strategy = RSIMeanReversionStrategy(RSIMeanReversionParams(rsi_period=14))
        assert strategy.warmup_bars() == 16

    def test_required_indicators(self) -> None:
        strategy = RSIMeanReversionStrategy(RSIMeanReversionParams(rsi_period=9))
        specs = strategy.required_indicators()
        assert specs["rsi"].kind == "rsi"
        assert specs["rsi"].params == {"period": 9}


class TestCrossingLogic:
    def test_long_on_cross_below_oversold_while_flat(self) -> None:
        strategy = RSIMeanReversionStrategy()  # oversold=30, exit=50
        ctx = _ctx([32.0, 28.0], FLAT_POSITION)
        signal = strategy.on_candle(ctx)
        assert signal is not None
        assert signal.direction == SignalDirection.LONG

    def test_no_signal_cross_below_oversold_while_already_long(self) -> None:
        strategy = RSIMeanReversionStrategy()
        ctx = _ctx([32.0, 28.0], PositionView(qty=5, avg_price=100.0))
        assert strategy.on_candle(ctx) is None

    def test_exit_on_cross_above_exit_level_while_long(self) -> None:
        strategy = RSIMeanReversionStrategy()
        ctx = _ctx([48.0, 52.0], PositionView(qty=5, avg_price=100.0))
        signal = strategy.on_candle(ctx)
        assert signal is not None
        assert signal.direction == SignalDirection.EXIT

    def test_no_signal_cross_above_exit_level_while_flat(self) -> None:
        strategy = RSIMeanReversionStrategy()
        ctx = _ctx([48.0, 52.0], FLAT_POSITION)
        assert strategy.on_candle(ctx) is None

    def test_no_signal_without_a_crossing(self) -> None:
        strategy = RSIMeanReversionStrategy()
        ctx = _ctx([40.0, 42.0], FLAT_POSITION)
        assert strategy.on_candle(ctx) is None

    def test_no_signal_during_warmup(self) -> None:
        strategy = RSIMeanReversionStrategy()
        ctx = _ctx([28.0], FLAT_POSITION)  # only one point -> no prev value
        assert strategy.on_candle(ctx) is None

    def test_custom_thresholds_respected(self) -> None:
        strategy = RSIMeanReversionStrategy(RSIMeanReversionParams(oversold=20.0, exit_level=60.0))
        # would cross the DEFAULT oversold (30) but not this custom one (20)
        ctx = _ctx([32.0, 25.0], FLAT_POSITION)
        assert strategy.on_candle(ctx) is None


class TestDeterministicReplay:
    """ROADMAP M7 testing plan: same data + params => same signals."""

    def test_same_inputs_produce_identical_trades(self) -> None:
        # Oscillating series designed to swing RSI through oversold and back.
        opens = [100.0] * 10 + [
            98,
            95,
            90,
            84,
            78,
            74,
            72,
            75,
            80,
            88,
            96,
            104,
            110,
            108,
            100,
            92,
            86,
            82,
            80,
            84,
            92,
            100,
        ]
        candles = synthetic_candles(opens)
        params = RSIMeanReversionParams(rsi_period=5, oversold=35.0, exit_level=55.0)
        result_1 = run_backtest(
            RSIMeanReversionStrategy(params),
            candles,
            initial_capital=Decimal("500000"),
            sizer=FixedFractionalSizer(Decimal("5.0")),
            cost_rates=CostConfig(),
            slippage_bps=Decimal("5"),
        )
        result_2 = run_backtest(
            RSIMeanReversionStrategy(params),
            candles,
            initial_capital=Decimal("500000"),
            sizer=FixedFractionalSizer(Decimal("5.0")),
            cost_rates=CostConfig(),
            slippage_bps=Decimal("5"),
        )

        assert result_1.trades == result_2.trades
        assert result_1.equity_curve == result_2.equity_curve
        assert len(result_1.trades) >= 1, "scenario must actually exercise a trade"
