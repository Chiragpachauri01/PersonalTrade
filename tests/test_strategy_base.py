from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd
import pytest

from personaltrade.backtest.indicator_bridge import BatchIndicatorView
from personaltrade.core.enums import SignalDirection
from personaltrade.strategy.base import FLAT_POSITION, PositionView, StrategyContext
from personaltrade.strategy.strategies.sma_crossover import SMACrossoverParams, SMACrossoverStrategy


class TestPositionView:
    def test_flat_long_short_properties(self) -> None:
        assert FLAT_POSITION.is_flat
        assert not FLAT_POSITION.is_long
        assert not FLAT_POSITION.is_short

        long_pos = PositionView(qty=10, avg_price=100.0)
        assert long_pos.is_long
        assert not long_pos.is_flat
        assert not long_pos.is_short

        short_pos = PositionView(qty=-5, avg_price=100.0)
        assert short_pos.is_short
        assert not short_pos.is_flat


class TestSMACrossoverStrategy:
    def test_rejects_fast_not_less_than_slow(self) -> None:
        with pytest.raises(ValueError, match="fast_period must be"):
            SMACrossoverStrategy(SMACrossoverParams(fast_period=10, slow_period=10))

    def test_warmup_bars_is_slow_plus_one(self) -> None:
        strategy = SMACrossoverStrategy(SMACrossoverParams(fast_period=5, slow_period=20))
        assert strategy.warmup_bars() == 21

    def test_required_indicators_reflect_params(self) -> None:
        strategy = SMACrossoverStrategy(SMACrossoverParams(fast_period=7, slow_period=25))
        specs = strategy.required_indicators()
        assert specs["fast"].kind == "sma"
        assert specs["fast"].params == {"period": 7}
        assert specs["slow"].kind == "sma"
        assert specs["slow"].params == {"period": 25}

    def test_default_params_when_none_given(self) -> None:
        strategy = SMACrossoverStrategy()
        assert strategy.params.fast_period == 10
        assert strategy.params.slow_period == 30

    def _ctx(
        self, fast_values: list[float], slow_values: list[float], position: PositionView
    ) -> StrategyContext:
        n = len(fast_values)
        series = {"fast": pd.Series(fast_values), "slow": pd.Series(slow_values)}
        candles = pd.DataFrame(
            {
                "close": fast_values,  # value unused by the crossover logic itself except last
            }
        )
        return StrategyContext(
            index=n - 1,
            ts=datetime(2026, 1, 1, tzinfo=UTC),
            candles=candles,
            indicators=BatchIndicatorView(series, n - 1),
            position=position,
        )

    def test_emits_long_on_cross_up_while_flat(self) -> None:
        strategy = SMACrossoverStrategy()
        # fast crosses above slow between the last two points
        ctx = self._ctx(fast_values=[9, 11], slow_values=[10, 10], position=FLAT_POSITION)
        signal = strategy.on_candle(ctx)
        assert signal is not None
        assert signal.direction == SignalDirection.LONG

    def test_no_signal_on_cross_up_while_already_long(self) -> None:
        strategy = SMACrossoverStrategy()
        ctx = self._ctx(
            fast_values=[9, 11], slow_values=[10, 10], position=PositionView(qty=5, avg_price=100)
        )
        assert strategy.on_candle(ctx) is None

    def test_emits_exit_on_cross_down_while_long(self) -> None:
        strategy = SMACrossoverStrategy()
        ctx = self._ctx(
            fast_values=[11, 9], slow_values=[10, 10], position=PositionView(qty=5, avg_price=100)
        )
        signal = strategy.on_candle(ctx)
        assert signal is not None
        assert signal.direction == SignalDirection.EXIT

    def test_no_signal_on_cross_down_while_flat(self) -> None:
        strategy = SMACrossoverStrategy()
        ctx = self._ctx(fast_values=[11, 9], slow_values=[10, 10], position=FLAT_POSITION)
        assert strategy.on_candle(ctx) is None

    def test_no_signal_without_a_crossing(self) -> None:
        strategy = SMACrossoverStrategy()
        ctx = self._ctx(fast_values=[12, 13], slow_values=[10, 10], position=FLAT_POSITION)
        assert strategy.on_candle(ctx) is None

    def test_no_signal_during_warmup(self) -> None:
        strategy = SMACrossoverStrategy()
        # only one point available -> no previous value to compare against
        series = {"fast": pd.Series([11.0]), "slow": pd.Series([10.0])}
        candles = pd.DataFrame({"close": [11.0]})
        ctx = StrategyContext(
            index=0,
            ts=datetime(2026, 1, 1, tzinfo=UTC),
            candles=candles,
            indicators=BatchIndicatorView(series, 0),
            position=FLAT_POSITION,
        )
        assert strategy.on_candle(ctx) is None
