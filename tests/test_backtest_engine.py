"""Backtest engine correctness: hand-traced fills, the position-transition
table, cash clamping, and the look-ahead sentinel (ROADMAP M6 testing plan).
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pandas as pd
import pytest

from personaltrade.backtest.engine import (
    BacktestError,
    _resolve_action,
    run_backtest,
)
from personaltrade.backtest.sizing import FixedFractionalSizer
from personaltrade.core.config import CostConfig
from personaltrade.core.enums import Segment, Side, SignalDirection
from personaltrade.data.providers.base import empty_candle_frame, normalize_candle_frame
from personaltrade.strategy.base import FLAT_POSITION, Signal
from personaltrade.strategy.strategies.sma_crossover import SMACrossoverParams, SMACrossoverStrategy
from tests.factories import FixedQtySizer, ScriptedStrategy, synthetic_candles

ZERO_COSTS = CostConfig(
    brokerage_pct=Decimal("0"),
    brokerage_max=Decimal("0"),
    stt_delivery_pct=Decimal("0"),
    stt_intraday_sell_pct=Decimal("0"),
    exchange_txn_pct=Decimal("0"),
    sebi_pct=Decimal("0"),
    stamp_duty_buy_delivery_pct=Decimal("0"),
    stamp_duty_buy_intraday_pct=Decimal("0"),
    gst_pct=Decimal("0"),
)


class TestPositionTransitionTable:
    @pytest.mark.parametrize(
        ("current_qty", "direction", "expected"),
        [
            (0, SignalDirection.LONG, (Side.BUY, False)),
            (0, SignalDirection.SHORT, (Side.SELL, False)),
            (0, SignalDirection.EXIT, None),
            (5, SignalDirection.LONG, None),  # already long
            (5, SignalDirection.SHORT, None),  # reversal unsupported
            (5, SignalDirection.EXIT, (Side.SELL, True)),
            (-5, SignalDirection.LONG, None),  # reversal unsupported
            (-5, SignalDirection.SHORT, None),  # already short
            (-5, SignalDirection.EXIT, (Side.BUY, True)),
        ],
    )
    def test_all_cells(
        self, current_qty: int, direction: SignalDirection, expected: tuple[Side, bool] | None
    ) -> None:
        assert _resolve_action(current_qty, direction) == expected


class TestHandTracedScenario:
    """Every number here is reconstructible by hand — see the docstring math."""

    def test_zero_cost_zero_slippage(self) -> None:
        # opens = [100,102,104,106,108,110]; close = open+1; zero costs/slippage.
        # schedule: LONG at bar1 -> fills bar2 open=104, qty=10 (fixed sizer)
        #           EXIT at bar3 -> fills bar4 open=108, qty=10 (full close)
        #
        # fill@2: net_amount = 104*10 = 1040 (BUY, zero cost) -> cash=100000-1040=98960
        #         qty=10, avg_price=1040/10=104
        # equity@2 (post-fill, marked at close2=105): 98960 + 10*105 = 100010
        # equity@0: cash=100000, qty=0 -> 100000
        # equity@1: no fill yet (pending set on THIS bar, not resolved yet) -> 100000
        # equity@3: no fill this bar -> 98960 + 10*close3(107) = 98960+1070=100030
        # fill@4: net_amount = 108*10 = 1080 (SELL) -> realized_pnl = 1080-104*10 = 40
        #         cash=98960+1080=100040, qty=0
        # equity@4 (post-fill, marked at close4=109): 100040 + 0 = 100040
        # equity@5: no fill -> 100040
        candles = synthetic_candles([100, 102, 104, 106, 108, 110])
        schedule = {
            1: Signal(SignalDirection.LONG, ref_price=103.0),
            3: Signal(SignalDirection.EXIT, ref_price=107.0),
        }
        result = run_backtest(
            ScriptedStrategy(schedule),
            candles,
            initial_capital=Decimal("100000"),
            sizer=FixedQtySizer(10),
            cost_rates=ZERO_COSTS,
            segment=Segment.DELIVERY,
            slippage_bps=Decimal("0"),
        )

        assert len(result.trades) == 2
        entry, exit_ = result.trades
        assert (entry.index, entry.side, entry.qty, entry.price) == (
            2,
            Side.BUY,
            10,
            Decimal("104"),
        )
        assert entry.realized_pnl is None
        assert (exit_.index, exit_.side, exit_.qty, exit_.price) == (
            4,
            Side.SELL,
            10,
            Decimal("108"),
        )
        assert exit_.realized_pnl == Decimal("40")

        expected_equity = [
            Decimal("100000"),
            Decimal("100000"),
            Decimal("100010"),
            Decimal("100030"),
            Decimal("100040"),
            Decimal("100040"),
        ]
        assert [p.equity for p in result.equity_curve] == expected_equity
        assert result.final_position == FLAT_POSITION
        assert result.unfilled_signals == []

    def test_invalid_reversal_produces_no_trade(self) -> None:
        # LONG@0 fills@1(open102,qty10); SHORT@2 while long -> ignored (no trade,
        # portfolio unchanged); EXIT@4 fills@5(open110), closing the ORIGINAL
        # long position untouched by the ignored SHORT signal.
        candles = synthetic_candles([100, 102, 104, 106, 108, 110, 112])
        schedule = {
            0: Signal(SignalDirection.LONG, ref_price=101.0),
            2: Signal(SignalDirection.SHORT, ref_price=105.0),
            4: Signal(SignalDirection.EXIT, ref_price=109.0),
        }
        result = run_backtest(
            ScriptedStrategy(schedule),
            candles,
            initial_capital=Decimal("100000"),
            sizer=FixedQtySizer(10),
            cost_rates=ZERO_COSTS,
            segment=Segment.DELIVERY,
            slippage_bps=Decimal("0"),
        )
        assert len(result.trades) == 2  # the SHORT signal produced no third trade
        entry, exit_ = result.trades
        assert (entry.index, entry.side, entry.price) == (1, Side.BUY, Decimal("102"))
        assert (exit_.index, exit_.side, exit_.price) == (5, Side.SELL, Decimal("110"))
        # realized pnl on the close reflects the ORIGINAL entry, proving the
        # ignored SHORT never touched avg_price/qty
        assert exit_.realized_pnl == Decimal("80")  # 110*10 - 102*10


class TestUnfilledSignal:
    def test_signal_on_final_bar_is_recorded_not_executed(self) -> None:
        candles = synthetic_candles([100, 102, 104, 106, 108, 110, 112])
        schedule = {6: Signal(SignalDirection.LONG, ref_price=113.0)}  # last bar index
        result = run_backtest(
            ScriptedStrategy(schedule),
            candles,
            initial_capital=Decimal("100000"),
            sizer=FixedQtySizer(10),
            cost_rates=ZERO_COSTS,
            segment=Segment.DELIVERY,
            slippage_bps=Decimal("0"),
        )
        assert result.trades == []
        assert len(result.unfilled_signals) == 1
        assert result.unfilled_signals[0].index == 6
        assert result.unfilled_signals[0].direction == SignalDirection.LONG


class TestCashClamping:
    def test_qty_reduced_to_fit_available_cash(self) -> None:
        # sizer wants 100 shares @ fill price 100 = 10000, but only 1000 cash
        # available -> clamps to floor(1000/100) = 10 shares.
        candles = synthetic_candles([100, 100, 100])
        schedule = {0: Signal(SignalDirection.LONG, ref_price=100.0)}
        result = run_backtest(
            ScriptedStrategy(schedule),
            candles,
            initial_capital=Decimal("1000"),
            sizer=FixedQtySizer(100),
            cost_rates=ZERO_COSTS,
            segment=Segment.DELIVERY,
            slippage_bps=Decimal("0"),
        )
        assert len(result.trades) == 1
        assert result.trades[0].qty == 10

    def test_insufficient_cash_for_even_one_share_skips_trade(self) -> None:
        candles = synthetic_candles([100, 100, 100])
        schedule = {0: Signal(SignalDirection.LONG, ref_price=100.0)}
        result = run_backtest(
            ScriptedStrategy(schedule),
            candles,
            initial_capital=Decimal("50"),  # less than one share at price 100
            sizer=FixedQtySizer(100),
            cost_rates=ZERO_COSTS,
            segment=Segment.DELIVERY,
            slippage_bps=Decimal("0"),
        )
        assert result.trades == []
        assert result.final_position == FLAT_POSITION


class TestCostAndSlippageIntegration:
    def test_fill_price_and_costs_match_the_cost_module_directly(self) -> None:
        """Cross-check against the already-tested cost module — not re-derived by hand."""
        from personaltrade.backtest.costs import calculate_costs

        rates = CostConfig()
        candles = synthetic_candles([1000.0, 1000.0, 1000.0])
        schedule = {0: Signal(SignalDirection.LONG, ref_price=1000.0)}
        slippage_bps = Decimal("5")
        result = run_backtest(
            ScriptedStrategy(schedule),
            candles,
            initial_capital=Decimal("500000"),
            sizer=FixedQtySizer(10),
            cost_rates=rates,
            segment=Segment.DELIVERY,
            slippage_bps=slippage_bps,
        )
        assert len(result.trades) == 1
        trade = result.trades[0]
        expected_fill_price = Decimal("1000") * (Decimal(1) + slippage_bps / Decimal(10000))
        assert trade.price == expected_fill_price
        assert trade.costs == calculate_costs(
            Side.BUY, expected_fill_price, 10, Segment.DELIVERY, rates
        )


class TestValidation:
    def test_empty_candles_rejected(self) -> None:
        with pytest.raises(BacktestError, match="empty"):
            run_backtest(
                ScriptedStrategy({}),
                empty_candle_frame(),
                initial_capital=Decimal("100000"),
                sizer=FixedQtySizer(1),
                cost_rates=ZERO_COSTS,
            )

    def test_nonpositive_capital_rejected(self) -> None:
        with pytest.raises(BacktestError, match="initial_capital"):
            run_backtest(
                ScriptedStrategy({}),
                synthetic_candles([100, 101]),
                initial_capital=Decimal("0"),
                sizer=FixedQtySizer(1),
                cost_rates=ZERO_COSTS,
            )


class TestNoLookAheadBias:
    def test_truncated_and_corrupted_tail_produce_identical_early_results(self) -> None:
        """The defining M6 test: results for bars [0, split) must be byte-identical
        whether or not wildly different data exists afterward (ROADMAP M6 —
        'future data poisoned -> must not change results')."""
        # Flat then strongly trending synthetic series, long enough to warm up
        # SMACrossoverStrategy(fast=2, slow=4) and reliably cross over.
        opens = [100.0] * 5 + [100, 101, 103, 106, 110, 115, 121, 128, 136, 145, 155]
        full = synthetic_candles(opens)
        split = 12

        honest = full.iloc[:split].reset_index(drop=True)
        tail = full.iloc[split:].copy().iloc[::-1].reset_index(drop=True)
        tail[["open", "high", "low", "close"]] = tail[["open", "high", "low", "close"]] * 10
        tail["ts"] = [honest["ts"].iloc[-1] + timedelta(days=i + 1) for i in range(len(tail))]
        corrupted = normalize_candle_frame(pd.concat([honest, tail], ignore_index=True))

        params = SMACrossoverParams(fast_period=2, slow_period=4)
        sizer = FixedFractionalSizer(Decimal("2.0"))
        cost_rates = CostConfig()
        result_honest = run_backtest(
            SMACrossoverStrategy(params),
            honest,
            initial_capital=Decimal("500000"),
            sizer=sizer,
            cost_rates=cost_rates,
            segment=Segment.DELIVERY,
            slippage_bps=Decimal("5"),
        )
        result_corrupted = run_backtest(
            SMACrossoverStrategy(params),
            corrupted,
            initial_capital=Decimal("500000"),
            sizer=sizer,
            cost_rates=cost_rates,
            segment=Segment.DELIVERY,
            slippage_bps=Decimal("5"),
        )

        # Equity is always well-defined for bars < split regardless of what
        # comes after (it depends only on fills from strictly earlier signals).
        assert result_honest.equity_curve == result_corrupted.equity_curve[:split]

        # Trades whose *signal* fired before the honest run's last bar are
        # unambiguously fillable in both runs (the honest run's very last bar
        # can't fill locally — see TestUnfilledSignal — so it's excluded to
        # avoid a false positive from that boundary, not from look-ahead).
        honest_trades = [t for t in result_honest.trades if t.signal_index < split - 1]
        corrupted_trades = [t for t in result_corrupted.trades if t.signal_index < split - 1]
        assert honest_trades == corrupted_trades
        assert len(honest_trades) >= 1, "scenario must actually exercise a trade"
