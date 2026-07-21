"""analytics/pnl.py: win_rate/expectancy/profit_factor edge cases, equity-curve
reconstruction, unrealized mark-to-market, and the combined PnLSummary
(ROADMAP M12 testing plan).
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from personaltrade.analytics.pnl import (
    compute_pnl_summary,
    equity_curve_from_trades,
    expectancy,
    profit_factor,
    unrealized_pnl,
    win_rate,
)
from personaltrade.core.config import CostConfig, PaperConfig
from personaltrade.core.enums import Interval, Mode, Side
from personaltrade.data.store.candles import CandleStore
from personaltrade.data.store.models import Instrument
from personaltrade.data.store.repos import InstrumentRepository, TradeRepository
from personaltrade.execution.broker import OrderRequest
from personaltrade.execution.paper.broker import PaperBroker
from tests.factories import FakeQuoteSource, synthetic_candles

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


def _paper_cfg() -> PaperConfig:
    return PaperConfig(slippage_bps=Decimal("0"), segment="DELIVERY", latency_ms=0)


@pytest.fixture()
def instrument(db_session: Session) -> Instrument:
    inst = InstrumentRepository(db_session).add(
        Instrument(
            symbol="AAA", exchange="NSE", instrument_key="NSE_EQ|AAA", tick_size=Decimal("0.05")
        )
    )
    db_session.flush()
    return inst


def _broker(session: Session, quotes: FakeQuoteSource, cash: str = "100000") -> PaperBroker:
    return PaperBroker(
        session,
        quotes,
        cost_rates=ZERO_COSTS,
        paper_cfg=_paper_cfg(),
        initial_cash=Decimal(cash),
    )


def _order(instrument: Instrument, side: Side, qty: int, client_order_id: str) -> OrderRequest:
    from personaltrade.core.enums import OrderType

    return OrderRequest(
        client_order_id=client_order_id,
        instrument_id=instrument.id,
        side=side,
        order_type=OrderType.MARKET,
        qty=qty,
        limit_price=None,
    )


class TestWinRate:
    def test_empty_is_zero(self) -> None:
        assert win_rate([]) == 0.0

    def test_all_wins(self) -> None:
        assert win_rate([10.0, 20.0, 5.0]) == 1.0

    def test_all_losses(self) -> None:
        assert win_rate([-10.0, -20.0]) == 0.0

    def test_mixed(self) -> None:
        assert win_rate([10.0, -5.0, 3.0, -1.0]) == 0.5

    def test_zero_pnl_does_not_count_as_a_win(self) -> None:
        assert win_rate([0.0, 10.0]) == 0.5


class TestExpectancy:
    def test_empty_is_zero(self) -> None:
        assert expectancy([]) == 0.0

    def test_average_of_pnls(self) -> None:
        assert expectancy([10.0, -4.0, 6.0]) == pytest.approx(4.0)


class TestProfitFactor:
    def test_empty_is_zero(self) -> None:
        assert profit_factor([]) == 0.0

    def test_no_losses_but_wins_is_infinite(self) -> None:
        assert profit_factor([10.0, 20.0]) == float("inf")

    def test_no_wins_no_losses_is_zero(self) -> None:
        assert profit_factor([]) == 0.0

    def test_only_losses_is_zero(self) -> None:
        assert profit_factor([-10.0, -5.0]) == 0.0

    def test_gross_win_over_gross_loss(self) -> None:
        # gross_win=30, gross_loss=10 -> 3.0
        assert profit_factor([20.0, 10.0, -10.0]) == pytest.approx(3.0)


class TestEquityCurveFromTrades:
    def test_empty_trades_gives_empty_series(self) -> None:
        assert equity_curve_from_trades(Decimal("100000"), []) == []

    def test_buy_then_sell_steps_cash_by_net_amount(
        self, db_session: Session, instrument: Instrument
    ) -> None:
        quotes = FakeQuoteSource({instrument.id: Decimal("100")})
        broker = _broker(db_session, quotes)
        broker.place_order(_order(instrument, Side.BUY, 10, "co-open"))
        quotes.prices[instrument.id] = Decimal("110")
        broker.place_order(_order(instrument, Side.SELL, 10, "co-close"))

        trades = TradeRepository(db_session).list_for_mode(Mode.PAPER)
        series = equity_curve_from_trades(Decimal("100000"), trades)

        values = [v for _, v in series]
        # seed point + one point per trade
        assert values[0] == 100000.0
        assert values[-2] == 99000.0  # after the buy: 100000 - 10*100
        assert values[-1] == 100100.0  # after the sell: 99000 + 10*110


class TestUnrealizedPnl:
    def test_marks_open_position_to_last_synced_close(
        self, db_session: Session, instrument: Instrument, tmp_path: Path
    ) -> None:
        quotes = FakeQuoteSource({instrument.id: Decimal("100")})
        broker = _broker(db_session, quotes)
        broker.place_order(_order(instrument, Side.BUY, 10, "co-1"))

        store = CandleStore(tmp_path / "candles")
        store.write("AAA", "NSE", Interval.D1, synthetic_candles([100, 105, 120]))
        # synthetic_candles close = open + 1 -> last close = 121

        result = unrealized_pnl(db_session, store, Mode.PAPER, Interval.D1)
        assert result == (Decimal("121") - Decimal("100")) * 10

    def test_no_synced_candles_contributes_zero(
        self, db_session: Session, instrument: Instrument, tmp_path: Path
    ) -> None:
        quotes = FakeQuoteSource({instrument.id: Decimal("100")})
        broker = _broker(db_session, quotes)
        broker.place_order(_order(instrument, Side.BUY, 10, "co-1"))

        store = CandleStore(tmp_path / "candles")  # nothing written
        assert unrealized_pnl(db_session, store, Mode.PAPER, Interval.D1) == Decimal("0")

    def test_no_open_positions_is_zero(self, db_session: Session, tmp_path: Path) -> None:
        store = CandleStore(tmp_path / "candles")
        assert unrealized_pnl(db_session, store, Mode.PAPER, Interval.D1) == Decimal("0")


class TestComputePnlSummary:
    def test_summary_combines_realized_and_unrealized(
        self, db_session: Session, instrument: Instrument
    ) -> None:
        quotes = FakeQuoteSource({instrument.id: Decimal("100")})
        broker = _broker(db_session, quotes)
        broker.place_order(_order(instrument, Side.BUY, 10, "co-open"))
        quotes.prices[instrument.id] = Decimal("110")
        broker.place_order(_order(instrument, Side.SELL, 10, "co-close"))

        all_trades = TradeRepository(db_session).list_for_mode(Mode.PAPER)
        realized_trades = [t for t in all_trades if t.realized_pnl is not None]
        assert len(realized_trades) == 1

        summary = compute_pnl_summary(
            Decimal("100000"), realized_trades, all_trades, unrealized=Decimal("50")
        )
        assert summary.realized_pnl == Decimal("100")  # 10 * (110-100), zero costs
        assert summary.unrealized_pnl == Decimal("50")
        assert summary.total_pnl == Decimal("150")
        assert summary.closed_trades == 1
        assert summary.win_rate == 1.0
        assert summary.expectancy == pytest.approx(100.0)
        assert summary.profit_factor == float("inf")

    def test_no_trades_gives_zeroed_summary(self) -> None:
        summary = compute_pnl_summary(Decimal("100000"), [], [], unrealized=Decimal("0"))
        assert summary.realized_pnl == Decimal("0")
        assert summary.closed_trades == 0
        assert summary.win_rate == 0.0
        assert summary.cagr == 0.0
        assert summary.sharpe == 0.0
        assert summary.max_drawdown == 0.0
