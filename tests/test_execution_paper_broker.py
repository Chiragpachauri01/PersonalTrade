"""Paper Broker correctness: order lifecycle, market/limit fills, cash-clamped
partial fills, cancellation, restart-safety, and cost-model parity with the
backtester (ROADMAP M9 testing plan).
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from personaltrade.backtest.costs import calculate_costs
from personaltrade.core.config import CostConfig, PaperConfig
from personaltrade.core.enums import OrderState, OrderType, Segment, Side
from personaltrade.data.store.db import build_engine, build_session_factory, session_scope
from personaltrade.data.store.models import Instrument
from personaltrade.data.store.repos import InstrumentRepository, OrderRepository
from personaltrade.execution.broker import OrderRequest, UnknownInstrument, UnknownOrder
from personaltrade.execution.paper.broker import PaperBroker
from tests.factories import FakeQuoteSource, ManualClock

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


def _paper_cfg(latency_ms: int = 0, slippage_bps: str = "0") -> PaperConfig:
    return PaperConfig(
        slippage_bps=Decimal(slippage_bps), segment="DELIVERY", latency_ms=latency_ms
    )


def _broker(
    session: Session,
    quotes: FakeQuoteSource,
    *,
    cash: str = "100000",
    cost_rates: CostConfig = ZERO_COSTS,
    paper_cfg: PaperConfig | None = None,
    clock: ManualClock | None = None,
) -> PaperBroker:
    return PaperBroker(
        session,
        quotes,
        cost_rates=cost_rates,
        paper_cfg=paper_cfg or _paper_cfg(),
        initial_cash=Decimal(cash),
        clock=clock,
    )


@pytest.fixture()
def instrument(db_session: Session) -> Instrument:
    inst = InstrumentRepository(db_session).add(
        Instrument(
            symbol="AAA", exchange="NSE", instrument_key="NSE_EQ|AAA", tick_size=Decimal("0.05")
        )
    )
    db_session.flush()
    return inst


def _request(
    instrument: Instrument,
    side: Side,
    qty: int,
    *,
    order_type: OrderType = OrderType.MARKET,
    limit_price: Decimal | None = None,
    client_order_id: str = "co-1",
) -> OrderRequest:
    return OrderRequest(
        client_order_id=client_order_id,
        instrument_id=instrument.id,
        side=side,
        order_type=order_type,
        qty=qty,
        limit_price=limit_price,
    )


class TestMarketOrders:
    def test_buy_fills_immediately(self, db_session: Session, instrument: Instrument) -> None:
        quotes = FakeQuoteSource({instrument.id: Decimal("100")})
        broker = _broker(db_session, quotes)
        ack = broker.place_order(_request(instrument, Side.BUY, 10))
        status = broker.get_order_status(ack.client_order_id)

        assert status.state == OrderState.FILLED
        assert status.filled_qty == 10
        assert status.avg_fill_price == Decimal("100")

        positions = broker.get_positions()
        assert len(positions) == 1
        assert positions[0].qty == 10
        assert positions[0].avg_price == Decimal("100")
        assert broker.get_funds().cash == Decimal("99000")  # 100000 - 10*100

    def test_sell_closing_realizes_pnl(self, db_session: Session, instrument: Instrument) -> None:
        quotes = FakeQuoteSource({instrument.id: Decimal("100")})
        broker = _broker(db_session, quotes)
        broker.place_order(_request(instrument, Side.BUY, 10, client_order_id="co-open"))

        quotes.prices[instrument.id] = Decimal("110")
        ack = broker.place_order(_request(instrument, Side.SELL, 10, client_order_id="co-close"))
        status = broker.get_order_status(ack.client_order_id)

        assert status.state == OrderState.FILLED
        positions = broker.get_positions()
        assert positions == []  # fully closed, no longer "open"
        # cash: 100000 -1000(buy) +1100(sell) = 100100
        assert broker.get_funds().cash == Decimal("100100")

    def test_no_quote_stays_open(self, db_session: Session, instrument: Instrument) -> None:
        broker = _broker(db_session, FakeQuoteSource({}))
        ack = broker.place_order(_request(instrument, Side.BUY, 10))
        status = broker.get_order_status(ack.client_order_id)
        assert status.state == OrderState.OPEN
        assert status.filled_qty == 0

    def test_unknown_instrument_raises(self, db_session: Session, instrument: Instrument) -> None:
        broker = _broker(db_session, FakeQuoteSource({}))
        bad_request = OrderRequest(
            client_order_id="co-x",
            instrument_id=999_999,
            side=Side.BUY,
            order_type=OrderType.MARKET,
            qty=10,
            limit_price=None,
        )
        with pytest.raises(UnknownInstrument):
            broker.place_order(bad_request)


class TestLimitOrders:
    def test_not_marketable_stays_resting(
        self, db_session: Session, instrument: Instrument
    ) -> None:
        # BUY limit 100, but market is at 110 -> not marketable.
        quotes = FakeQuoteSource({instrument.id: Decimal("110")})
        broker = _broker(db_session, quotes)
        ack = broker.place_order(
            _request(
                instrument, Side.BUY, 10, order_type=OrderType.LIMIT, limit_price=Decimal("100")
            )
        )
        status = broker.get_order_status(ack.client_order_id)
        assert status.state == OrderState.OPEN
        assert status.filled_qty == 0

    def test_marketable_fills_at_better_of_ltp_and_limit(
        self, db_session: Session, instrument: Instrument
    ) -> None:
        # BUY limit 100, market at 95 -> fills at 95 (better than the limit), not 100.
        quotes = FakeQuoteSource({instrument.id: Decimal("95")})
        broker = _broker(db_session, quotes)
        ack = broker.place_order(
            _request(
                instrument, Side.BUY, 10, order_type=OrderType.LIMIT, limit_price=Decimal("100")
            )
        )
        status = broker.get_order_status(ack.client_order_id)
        assert status.state == OrderState.FILLED
        assert status.avg_fill_price == Decimal("95")

    def test_check_resting_orders_fills_once_price_crosses(
        self, db_session: Session, instrument: Instrument
    ) -> None:
        quotes = FakeQuoteSource({instrument.id: Decimal("110")})
        broker = _broker(db_session, quotes)
        ack = broker.place_order(
            _request(
                instrument, Side.BUY, 10, order_type=OrderType.LIMIT, limit_price=Decimal("100")
            )
        )
        assert broker.check_resting_orders() == []  # still not marketable
        assert broker.get_order_status(ack.client_order_id).state == OrderState.OPEN

        quotes.prices[instrument.id] = Decimal("95")
        updates = broker.check_resting_orders()
        assert len(updates) == 1
        assert updates[0].to_state == OrderState.FILLED
        assert broker.get_order_status(ack.client_order_id).state == OrderState.FILLED


class TestCashClamping:
    def test_buy_clamped_to_affordable_qty(
        self, db_session: Session, instrument: Instrument
    ) -> None:
        quotes = FakeQuoteSource({instrument.id: Decimal("100")})
        broker = _broker(db_session, quotes, cash="1000")  # affords exactly 10 @ 100, zero costs
        ack = broker.place_order(_request(instrument, Side.BUY, 100))
        status = broker.get_order_status(ack.client_order_id)
        assert status.state == OrderState.PARTIALLY_FILLED
        assert status.filled_qty == 10
        assert broker.get_funds().cash == Decimal("0")

    def test_zero_affordable_qty_cancels(self, db_session: Session, instrument: Instrument) -> None:
        quotes = FakeQuoteSource({instrument.id: Decimal("100")})
        broker = _broker(db_session, quotes, cash="50")  # can't afford even 1 share
        ack = broker.place_order(_request(instrument, Side.BUY, 10))
        status = broker.get_order_status(ack.client_order_id)
        assert status.state == OrderState.CANCELLED
        assert status.filled_qty == 0

    def test_partial_fill_then_top_up_completes_with_weighted_avg_price(
        self, db_session: Session, instrument: Instrument
    ) -> None:
        quotes = FakeQuoteSource({instrument.id: Decimal("100")})
        broker = _broker(db_session, quotes, cash="1000")
        ack = broker.place_order(_request(instrument, Side.BUY, 20))
        assert broker.get_order_status(ack.client_order_id).filled_qty == 10

        broker.account.cash += Decimal("2000")  # simulate funds added between attempts
        quotes.prices[instrument.id] = Decimal("105")
        broker.check_resting_orders()

        status = broker.get_order_status(ack.client_order_id)
        assert status.state == OrderState.FILLED
        assert status.filled_qty == 20
        # (10*100 + 10*105) / 20 = 102.5
        assert status.avg_fill_price == Decimal("102.5")


class TestCancel:
    def test_cancel_open_order(self, db_session: Session, instrument: Instrument) -> None:
        broker = _broker(db_session, FakeQuoteSource({}))
        ack = broker.place_order(_request(instrument, Side.BUY, 10))
        broker.cancel_order(ack.client_order_id)
        assert broker.get_order_status(ack.client_order_id).state == OrderState.CANCELLED

    def test_cancel_already_terminal_is_a_noop(
        self, db_session: Session, instrument: Instrument
    ) -> None:
        quotes = FakeQuoteSource({instrument.id: Decimal("100")})
        broker = _broker(db_session, quotes)
        ack = broker.place_order(_request(instrument, Side.BUY, 10))
        assert broker.get_order_status(ack.client_order_id).state == OrderState.FILLED
        broker.cancel_order(ack.client_order_id)  # must not raise
        assert broker.get_order_status(ack.client_order_id).state == OrderState.FILLED

    def test_cancel_unknown_order_raises(self, db_session: Session, instrument: Instrument) -> None:
        broker = _broker(db_session, FakeQuoteSource({}))
        with pytest.raises(UnknownOrder):
            broker.cancel_order("does-not-exist")


class TestGetOrderStatus:
    def test_unknown_order_raises(self, db_session: Session, instrument: Instrument) -> None:
        broker = _broker(db_session, FakeQuoteSource({}))
        with pytest.raises(UnknownOrder):
            broker.get_order_status("does-not-exist")

    def test_avg_fill_price_none_when_unfilled(
        self, db_session: Session, instrument: Instrument
    ) -> None:
        broker = _broker(db_session, FakeQuoteSource({}))
        ack = broker.place_order(_request(instrument, Side.BUY, 10))
        assert broker.get_order_status(ack.client_order_id).avg_fill_price is None


class TestPositionsAndFunds:
    def test_get_positions_excludes_flat_instruments(
        self, db_session: Session, instrument: Instrument
    ) -> None:
        other = InstrumentRepository(db_session).add(
            Instrument(
                symbol="BBB", exchange="NSE", instrument_key="NSE_EQ|BBB", tick_size=Decimal("0.05")
            )
        )
        db_session.flush()
        quotes = FakeQuoteSource({instrument.id: Decimal("100"), other.id: Decimal("50")})
        broker = _broker(db_session, quotes)
        broker.place_order(_request(instrument, Side.BUY, 10))
        # never trade `other` -> no position row exists for it at all
        positions = broker.get_positions()
        assert len(positions) == 1
        assert positions[0].instrument_id == instrument.id

    def test_funds_marks_to_market_with_current_quote(
        self, db_session: Session, instrument: Instrument
    ) -> None:
        quotes = FakeQuoteSource({instrument.id: Decimal("100")})
        broker = _broker(db_session, quotes)
        broker.place_order(_request(instrument, Side.BUY, 10))
        quotes.prices[instrument.id] = Decimal("120")
        funds = broker.get_funds()
        assert funds.cash == Decimal("99000")
        assert funds.equity == Decimal("99000") + Decimal("10") * Decimal("120")

    def test_funds_falls_back_to_avg_price_without_a_quote(
        self, db_session: Session, instrument: Instrument
    ) -> None:
        quotes = FakeQuoteSource({instrument.id: Decimal("100")})
        broker = _broker(db_session, quotes)
        broker.place_order(_request(instrument, Side.BUY, 10))
        del quotes.prices[instrument.id]  # feed goes stale
        funds = broker.get_funds()
        assert funds.equity == Decimal("99000") + Decimal("10") * Decimal("100")  # avg_price basis


class TestLatency:
    def test_fill_timestamp_is_offset_from_order_placement(
        self, db_session: Session, instrument: Instrument
    ) -> None:
        clock = ManualClock()
        placed_at = clock.now()
        quotes = FakeQuoteSource({instrument.id: Decimal("100")})
        broker = _broker(db_session, quotes, paper_cfg=_paper_cfg(latency_ms=500), clock=clock)
        broker.place_order(_request(instrument, Side.BUY, 10))

        order = OrderRepository(db_session).get_by_client_order_id("co-1")
        assert order is not None
        [trade] = order.trades
        assert trade.executed_at == placed_at + timedelta(milliseconds=500)


class TestCostParity:
    def test_fill_costs_match_calculate_costs_directly(
        self, db_session: Session, instrument: Instrument
    ) -> None:
        real_costs = CostConfig()  # non-zero, realistic rates
        quotes = FakeQuoteSource({instrument.id: Decimal("1301.23")})
        broker = _broker(db_session, quotes, cost_rates=real_costs, cash="500000")
        ack = broker.place_order(_request(instrument, Side.BUY, 10))
        status = broker.get_order_status(ack.client_order_id)
        assert status.avg_fill_price is not None

        order = OrderRepository(db_session).get_by_client_order_id(ack.client_order_id)
        assert order is not None
        [trade] = order.trades

        expected = calculate_costs(
            Side.BUY, status.avg_fill_price, 10, Segment.DELIVERY, real_costs
        )
        assert trade.brokerage == expected.brokerage
        assert trade.stt == expected.stt
        assert trade.stamp_duty == expected.stamp_duty
        assert trade.exchange_charges == expected.exchange_charges
        assert trade.sebi_charges == expected.sebi_charges
        assert trade.gst == expected.gst
        assert trade.net_amount == expected.net_amount


class TestStreamOrderUpdates:
    def test_drains_queue_once(self, db_session: Session, instrument: Instrument) -> None:
        quotes = FakeQuoteSource({instrument.id: Decimal("100")})
        broker = _broker(db_session, quotes)
        broker.place_order(_request(instrument, Side.BUY, 10))

        async def _collect() -> list[OrderState]:
            return [u.to_state async for u in broker.stream_order_updates()]

        first = asyncio.run(_collect())
        assert first == [OrderState.OPEN, OrderState.FILLED]
        second = asyncio.run(_collect())
        assert second == []


class TestRestartSafety:
    def test_position_and_funds_survive_reconstruction(self, tmp_path: Path) -> None:
        db_path = tmp_path / "restart.db"
        engine = build_engine(db_path)
        from personaltrade.data.store.models import Base

        Base.metadata.create_all(engine)
        factory = build_session_factory(engine)

        quotes = FakeQuoteSource()
        with session_scope(factory) as session:
            inst = InstrumentRepository(session).add(
                Instrument(
                    symbol="AAA",
                    exchange="NSE",
                    instrument_key="NSE_EQ|AAA",
                    tick_size=Decimal("0.05"),
                )
            )
            session.flush()
            quotes.prices[inst.id] = Decimal("100")
            broker = _broker(session, quotes)
            broker.place_order(_request(inst, Side.BUY, 10))
        engine.dispose()  # simulate process restart

        engine2 = build_engine(db_path)
        factory2 = build_session_factory(engine2)
        with session_scope(factory2) as session2:
            inst2 = InstrumentRepository(session2).get_by_symbol("AAA", "NSE")
            assert inst2 is not None
            quotes.prices[inst2.id] = Decimal("100")
            broker2 = _broker(session2, quotes, cash="0")  # cash is read from the persisted row
            status = broker2.get_order_status("co-1")
            assert status.state == OrderState.FILLED
            assert status.filled_qty == 10
            positions = broker2.get_positions()
            assert len(positions) == 1
            assert positions[0].qty == 10
            assert broker2.get_funds().cash == Decimal("99000")
        engine2.dispose()
