"""intelligence/recommendation/screener.py: `latest_signal` must agree with
what the backtest engine would produce on the same final bar — same warmup
gating (ADR-015 point 1: max(warmup_bars(), first_all_valid_index)), same
"no signal" cases — since it reuses the identical indicator bridge.
"""

from __future__ import annotations

from personaltrade.core.enums import SignalDirection
from personaltrade.data.providers.base import empty_candle_frame
from personaltrade.intelligence.recommendation.screener import latest_signal
from personaltrade.strategy.base import FLAT_POSITION, PositionView, Signal
from personaltrade.strategy.strategies.sma_crossover import SMACrossoverParams, SMACrossoverStrategy
from tests.factories import ScriptedStrategy, synthetic_candles


class TestLatestSignal:
    def test_empty_candles_returns_none(self) -> None:
        strategy = SMACrossoverStrategy()
        assert latest_signal(strategy, empty_candle_frame(), FLAT_POSITION) is None

    def test_still_warming_up_returns_none(self) -> None:
        strategy = SMACrossoverStrategy(SMACrossoverParams(fast_period=2, slow_period=4))
        # warmup_bars() == 5; only 4 bars of history — not enough.
        candles = synthetic_candles([100, 101, 102, 103])
        assert latest_signal(strategy, candles, FLAT_POSITION) is None

    def test_returns_the_signal_on_the_final_bar(self) -> None:
        strategy = ScriptedStrategy({4: Signal(SignalDirection.LONG, 111.0, {})})
        candles = synthetic_candles([100, 101, 102, 103, 104])

        signal = latest_signal(strategy, candles, FLAT_POSITION)

        assert signal == Signal(SignalDirection.LONG, 111.0, {})

    def test_returns_none_when_strategy_declines_to_act_on_the_final_bar(self) -> None:
        strategy = ScriptedStrategy({0: Signal(SignalDirection.LONG, 101.0, {})})
        candles = synthetic_candles([100, 101, 102, 103, 104])

        assert latest_signal(strategy, candles, FLAT_POSITION) is None

    def test_real_strategy_crossover_on_final_bar(self) -> None:
        """A genuine golden-cross setup: fast SMA(2) crosses above slow
        SMA(4) exactly on the final bar, starting flat."""
        strategy = SMACrossoverStrategy(SMACrossoverParams(fast_period=2, slow_period=4))
        # closes: 101,102,103,104,105,106,120 (synthetic_candles close = open+1)
        candles = synthetic_candles([100, 99, 98, 97, 96, 95, 150])

        signal = latest_signal(strategy, candles, FLAT_POSITION)

        assert signal is not None
        assert signal.direction == SignalDirection.LONG

    def test_position_is_passed_through_to_the_strategy(self) -> None:
        """A LONG-only crossover strategy in an already-long position must
        not re-emit LONG — confirms `position` genuinely reaches on_candle()."""
        strategy = SMACrossoverStrategy(SMACrossoverParams(fast_period=2, slow_period=4))
        candles = synthetic_candles([100, 99, 98, 97, 96, 95, 150])
        already_long = PositionView(qty=10, avg_price=100.0)

        signal = latest_signal(strategy, candles, already_long)

        assert signal is None
