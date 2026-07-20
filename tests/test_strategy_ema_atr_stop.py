"""EMAAtrStopStrategy: the first stateful strategy — tests specifically target
the two design traps called out in ADR-016: the stop must anchor to the
*actual fill price* (ctx.position.avg_price), never the signal-time close,
and state must self-heal whenever the position goes flat (cross-symbol/
cross-run safety).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pandas as pd
import pytest

from personaltrade.backtest.engine import run_backtest
from personaltrade.backtest.indicator_bridge import BatchIndicatorView
from personaltrade.backtest.sizing import FixedFractionalSizer
from personaltrade.core.config import CostConfig
from personaltrade.core.enums import SignalDirection
from personaltrade.strategy.base import FLAT_POSITION, PositionView, StrategyContext
from personaltrade.strategy.strategies.ema_atr_stop import EMAAtrStopParams, EMAAtrStopStrategy
from tests.factories import synthetic_candles


def _ctx(
    fast: list[float],
    slow: list[float],
    atr: list[float],
    close: list[float],
    position: PositionView,
) -> StrategyContext:
    n = len(close)
    series = {
        "fast": pd.Series(fast),
        "slow": pd.Series(slow),
        "atr": pd.Series(atr),
    }
    return StrategyContext(
        index=n - 1,
        ts=datetime(2026, 1, 1, tzinfo=UTC),
        candles=pd.DataFrame({"close": close}),
        indicators=BatchIndicatorView(series, n - 1),
        position=position,
    )


class TestConstructionAndDeclaration:
    def test_rejects_fast_not_less_than_slow(self) -> None:
        with pytest.raises(ValueError, match="fast_period must be"):
            EMAAtrStopStrategy(EMAAtrStopParams(fast_period=20, slow_period=20))

    def test_warmup_is_max_of_slow_and_atr_plus_one(self) -> None:
        strategy = EMAAtrStopStrategy(EMAAtrStopParams(slow_period=26, atr_period=40))
        assert strategy.warmup_bars() == 41
        strategy2 = EMAAtrStopStrategy(EMAAtrStopParams(slow_period=50, atr_period=14))
        assert strategy2.warmup_bars() == 51

    def test_required_indicators(self) -> None:
        strategy = EMAAtrStopStrategy(
            EMAAtrStopParams(fast_period=5, slow_period=15, atr_period=7, atr_multiplier=3.0)
        )
        specs = strategy.required_indicators()
        assert specs["fast"].kind == "ema"
        assert specs["fast"].params == {"period": 5}
        assert specs["slow"].kind == "ema"
        assert specs["slow"].params == {"period": 15}
        assert specs["atr"].kind == "atr"
        assert specs["atr"].params == {"period": 7}


class TestEntryAndStopAnchoring:
    def test_long_on_cross_up_while_flat(self) -> None:
        strategy = EMAAtrStopStrategy()
        ctx = _ctx(
            fast=[9, 11], slow=[10, 10], atr=[2.0, 2.0], close=[99, 101], position=FLAT_POSITION
        )
        signal = strategy.on_candle(ctx)
        assert signal is not None
        assert signal.direction == SignalDirection.LONG

    def test_stop_anchors_to_actual_fill_price_not_signal_close(self) -> None:
        """Discriminating test: if the stop were wrongly anchored to the
        signal bar's close (100) instead of the real fill (avg_price=105),
        a close of 97 would NOT trigger an exit (97 > 100-6=94). Under the
        correct implementation it DOES (97 <= 105-6=99)."""
        strategy = EMAAtrStopStrategy(
            EMAAtrStopParams(fast_period=2, slow_period=3, atr_period=2, atr_multiplier=2.0)
        )

        # Bar B: first bar the strategy observes itself long. No cross-down
        # (fast stays above slow), so this call only sets the stop.
        ctx_b = _ctx(
            fast=[15, 16],
            slow=[10, 10],
            atr=[3.0, 3.0],
            close=[100, 103],
            position=PositionView(qty=10, avg_price=105.0),  # the REAL fill price
        )
        assert strategy.on_candle(ctx_b) is None
        assert strategy._stop == pytest.approx(105.0 - 2.0 * 3.0)  # 99.0, not 94.0

        # Bar C: close=97 sits between the wrong stop (94) and the correct
        # one (99) -> must exit under the correct, avg_price-anchored stop.
        ctx_c = _ctx(
            fast=[16, 16],
            slow=[10, 10],
            atr=[3.0, 3.0],
            close=[103, 97],
            position=PositionView(qty=10, avg_price=105.0),
        )
        signal = strategy.on_candle(ctx_c)
        assert signal is not None
        assert signal.direction == SignalDirection.EXIT
        assert signal.context["reason"] == "stop"

    def test_no_exit_when_close_stays_above_the_correct_stop(self) -> None:
        strategy = EMAAtrStopStrategy(
            EMAAtrStopParams(fast_period=2, slow_period=3, atr_period=2, atr_multiplier=2.0)
        )
        ctx_b = _ctx(
            fast=[15, 16],
            slow=[10, 10],
            atr=[3.0, 3.0],
            close=[100, 103],
            position=PositionView(qty=10, avg_price=105.0),
        )
        strategy.on_candle(ctx_b)  # sets stop = 99.0
        ctx_c = _ctx(
            fast=[16, 16],
            slow=[10, 10],
            atr=[3.0, 3.0],
            close=[103, 99.5],
            position=PositionView(qty=10, avg_price=105.0),
        )
        assert strategy.on_candle(ctx_c) is None

    def test_exit_on_cross_down_even_above_stop(self) -> None:
        strategy = EMAAtrStopStrategy(
            EMAAtrStopParams(fast_period=2, slow_period=3, atr_period=2, atr_multiplier=2.0)
        )
        ctx_b = _ctx(
            fast=[15, 16],
            slow=[10, 10],
            atr=[3.0, 3.0],
            close=[100, 103],
            position=PositionView(qty=10, avg_price=105.0),
        )
        strategy.on_candle(ctx_b)  # stop = 99.0
        # fast crosses below slow; close (110) is nowhere near the stop
        ctx_c = _ctx(
            fast=[16, 9],
            slow=[10, 10],
            atr=[3.0, 3.0],
            close=[103, 110],
            position=PositionView(qty=10, avg_price=105.0),
        )
        signal = strategy.on_candle(ctx_c)
        assert signal is not None
        assert signal.direction == SignalDirection.EXIT
        assert signal.context["reason"] == "cross_down"


class TestStateResetsOnFlat:
    def test_dirty_stop_state_clears_on_a_flat_context(self) -> None:
        """Cross-symbol / cross-run safety (ADR-016): a strategy instance
        that ends one run mid-position must not carry that stop into a
        fresh, flat-starting context (as it would after backtest/run.py
        constructs a new instance per symbol, or if it didn't)."""
        strategy = EMAAtrStopStrategy()
        strategy._stop = 12345.0  # simulate leftover state from a prior run

        ctx = _ctx(
            fast=[9, 9.5], slow=[10, 10], atr=[2.0, 2.0], close=[99, 99.5], position=FLAT_POSITION
        )
        strategy.on_candle(ctx)
        assert strategy._stop is None

    def test_stop_cleared_immediately_after_exit_signal(self) -> None:
        strategy = EMAAtrStopStrategy(
            EMAAtrStopParams(fast_period=2, slow_period=3, atr_period=2, atr_multiplier=2.0)
        )
        ctx_b = _ctx(
            fast=[15, 16],
            slow=[10, 10],
            atr=[3.0, 3.0],
            close=[100, 103],
            position=PositionView(qty=10, avg_price=105.0),
        )
        strategy.on_candle(ctx_b)
        ctx_exit = _ctx(
            fast=[16, 16],
            slow=[10, 10],
            atr=[3.0, 3.0],
            close=[103, 97],
            position=PositionView(qty=10, avg_price=105.0),
        )
        strategy.on_candle(ctx_exit)
        assert strategy._stop is None


class TestDeterministicReplay:
    """ROADMAP M7 testing plan: same data + params => same signals."""

    def test_same_inputs_produce_identical_trades(self) -> None:
        # Flat, then a sustained uptrend (room for the crossover to fire and
        # fill), then a sharp reversal (guarantees a stop or cross-down exit).
        opens = (
            [100.0] * 8
            + [102, 105, 109, 114, 120, 127, 135, 144, 154, 165]
            + [150, 130, 110, 95, 85, 80]
        )
        candles = synthetic_candles(opens)
        params = EMAAtrStopParams(fast_period=3, slow_period=6, atr_period=5, atr_multiplier=2.0)
        result_1 = run_backtest(
            EMAAtrStopStrategy(params),
            candles,
            initial_capital=Decimal("500000"),
            sizer=FixedFractionalSizer(Decimal("5.0")),
            cost_rates=CostConfig(),
            slippage_bps=Decimal("5"),
        )
        result_2 = run_backtest(
            EMAAtrStopStrategy(params),
            candles,
            initial_capital=Decimal("500000"),
            sizer=FixedFractionalSizer(Decimal("5.0")),
            cost_rates=CostConfig(),
            slippage_bps=Decimal("5"),
        )

        assert result_1.trades == result_2.trades
        assert result_1.equity_curve == result_2.equity_curve
        assert len(result_1.trades) >= 1, "scenario must actually exercise a trade"
