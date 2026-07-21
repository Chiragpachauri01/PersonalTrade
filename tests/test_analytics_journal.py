"""analytics/journal.py: round-trip episode grouping (incl. partial-fill
multi-leg entries), since-filtering-by-exit-time correctness, and
entry/exit signal-context capture (ROADMAP M12 testing plan).
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from personaltrade.analytics.journal import build_journal
from personaltrade.core.config import CostConfig, PaperConfig
from personaltrade.core.enums import Mode, OrderType, Side, SignalDirection, SignalStatus
from personaltrade.data.store.models import Instrument, Signal, StrategyRun
from personaltrade.data.store.repos import (
    InstrumentRepository,
    OrderRepository,
    SignalRepository,
    StrategyRunRepository,
)
from personaltrade.execution.broker import OrderRequest
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


def _broker(
    session: Session,
    quotes: FakeQuoteSource,
    cash: str = "100000",
    cost_rates: CostConfig = ZERO_COSTS,
    clock: ManualClock | None = None,
) -> PaperBroker:
    return PaperBroker(
        session,
        quotes,
        cost_rates=cost_rates,
        paper_cfg=_paper_cfg(),
        initial_cash=Decimal(cash),
        clock=clock,
    )


def _order(instrument: Instrument, side: Side, qty: int, client_order_id: str) -> OrderRequest:
    return OrderRequest(
        client_order_id=client_order_id,
        instrument_id=instrument.id,
        side=side,
        order_type=OrderType.MARKET,
        qty=qty,
        limit_price=None,
    )


def _attach_signal(
    session: Session, instrument: Instrument, client_order_id: str, context: dict[str, float]
) -> None:
    """Mimics the Orchestrator's post-hoc Order.signal_id link (M12 retrofit) —
    PaperBroker itself has no notion of signals, so tests that need
    entry/exit context wire it up the same way the real orchestrator does.
    """
    run = StrategyRunRepository(session).add(
        StrategyRun(strategy_name="test-strategy", params={}, mode=Mode.PAPER)
    )
    session.flush()
    signal = SignalRepository(session).add(
        Signal(
            instrument_id=instrument.id,
            strategy_run_id=run.id,
            direction=SignalDirection.LONG,
            ref_price=Decimal("100"),
            context=context,
            status=SignalStatus.APPROVED,
        )
    )
    session.flush()
    order = OrderRepository(session).get_by_client_order_id(client_order_id)
    assert order is not None
    order.signal_id = signal.id
    session.flush()


class TestSingleRoundTrip:
    def test_buy_then_sell_produces_one_entry(
        self, db_session: Session, instrument: Instrument
    ) -> None:
        quotes = FakeQuoteSource({instrument.id: Decimal("100")})
        broker = _broker(db_session, quotes)
        broker.place_order(_order(instrument, Side.BUY, 10, "co-open"))
        quotes.prices[instrument.id] = Decimal("110")
        broker.place_order(_order(instrument, Side.SELL, 10, "co-close"))

        entries = build_journal(db_session, Mode.PAPER)
        assert len(entries) == 1
        entry = entries[0]
        assert entry.symbol == "AAA"
        assert entry.side == Side.BUY
        assert entry.qty == 10
        assert entry.entry_price == Decimal("100")
        assert entry.exit_price == Decimal("110")
        assert entry.realized_pnl == Decimal("100")  # 10 * (110-100), zero costs
        assert entry.total_costs == Decimal("0")

    def test_total_costs_matches_sum_of_trade_legs(
        self, db_session: Session, instrument: Instrument
    ) -> None:
        real_costs = CostConfig()  # realistic, non-zero rates
        quotes = FakeQuoteSource({instrument.id: Decimal("1300")})
        broker = _broker(db_session, quotes, cash="500000", cost_rates=real_costs)
        broker.place_order(_order(instrument, Side.BUY, 10, "co-open"))
        quotes.prices[instrument.id] = Decimal("1320")
        broker.place_order(_order(instrument, Side.SELL, 10, "co-close"))

        order = OrderRepository(db_session).get_by_client_order_id("co-open")
        assert order is not None
        [buy_trade] = order.trades
        close_order = OrderRepository(db_session).get_by_client_order_id("co-close")
        assert close_order is not None
        [sell_trade] = close_order.trades
        expected_costs = sum(
            (
                t.brokerage + t.stt + t.stamp_duty + t.gst + t.exchange_charges + t.sebi_charges
                for t in (buy_trade, sell_trade)
            ),
            Decimal("0"),
        )

        [entry] = build_journal(db_session, Mode.PAPER)
        assert entry.total_costs == expected_costs

    def test_still_open_position_produces_no_entry(
        self, db_session: Session, instrument: Instrument
    ) -> None:
        quotes = FakeQuoteSource({instrument.id: Decimal("100")})
        broker = _broker(db_session, quotes)
        broker.place_order(_order(instrument, Side.BUY, 10, "co-open"))

        assert build_journal(db_session, Mode.PAPER) == []


class TestPartialFillMultiLegEntry:
    def test_two_entry_legs_average_into_one_episode(
        self, db_session: Session, instrument: Instrument
    ) -> None:
        quotes = FakeQuoteSource({instrument.id: Decimal("100")})
        # cash affords exactly 10 of 20 requested -> partial fill (leg 1)
        broker = _broker(db_session, quotes, cash="1000")
        broker.place_order(_order(instrument, Side.BUY, 20, "co-open"))
        assert broker.get_order_status("co-open").filled_qty == 10

        # top up cash, price moves, resting order completes (leg 2)
        broker.account.cash += Decimal("2000")
        quotes.prices[instrument.id] = Decimal("105")
        broker.check_resting_orders()
        assert broker.get_order_status("co-open").filled_qty == 20

        quotes.prices[instrument.id] = Decimal("110")
        broker.place_order(_order(instrument, Side.SELL, 20, "co-close"))

        [entry] = build_journal(db_session, Mode.PAPER)
        assert entry.qty == 20
        # (10*100 + 10*105) / 20 = 102.5
        assert entry.entry_price == Decimal("102.5")
        assert entry.exit_price == Decimal("110")


class TestSinceFiltering:
    def test_filters_by_exit_time_not_entry_time(
        self, db_session: Session, instrument: Instrument
    ) -> None:
        clock = ManualClock()
        quotes = FakeQuoteSource({instrument.id: Decimal("100")})
        broker = _broker(db_session, quotes, clock=clock)

        # Episode 1: entry+exit both before the cutoff.
        broker.place_order(_order(instrument, Side.BUY, 10, "co-1-open"))
        clock.advance(hours=1)
        broker.place_order(_order(instrument, Side.SELL, 10, "co-1-close"))
        clock.advance(hours=1)

        cutoff = clock.now()
        clock.advance(hours=1)

        # Episode 2: entry before cutoff, exit after -> exit_at is what matters.
        broker.place_order(_order(instrument, Side.BUY, 10, "co-2-open"))
        clock.advance(hours=1)
        broker.place_order(_order(instrument, Side.SELL, 10, "co-2-close"))

        entries = build_journal(db_session, Mode.PAPER, since=cutoff)
        assert len(entries) == 1
        assert entries[0].exit_at >= cutoff

    def test_no_since_returns_all_episodes(
        self, db_session: Session, instrument: Instrument
    ) -> None:
        clock = ManualClock()
        quotes = FakeQuoteSource({instrument.id: Decimal("100")})
        broker = _broker(db_session, quotes, clock=clock)
        broker.place_order(_order(instrument, Side.BUY, 10, "co-1-open"))
        clock.advance(hours=1)
        broker.place_order(_order(instrument, Side.SELL, 10, "co-1-close"))

        assert len(build_journal(db_session, Mode.PAPER)) == 1


class TestSignalContextCapture:
    def test_entry_and_exit_context_come_from_their_own_signals(
        self, db_session: Session, instrument: Instrument
    ) -> None:
        quotes = FakeQuoteSource({instrument.id: Decimal("100")})
        broker = _broker(db_session, quotes)
        broker.place_order(_order(instrument, Side.BUY, 10, "co-open"))
        _attach_signal(db_session, instrument, "co-open", {"rsi": 28.0})

        quotes.prices[instrument.id] = Decimal("110")
        broker.place_order(_order(instrument, Side.SELL, 10, "co-close"))
        _attach_signal(db_session, instrument, "co-close", {"rsi": 71.0})

        [entry] = build_journal(db_session, Mode.PAPER)
        assert entry.entry_context == {"rsi": 28.0}
        assert entry.exit_context == {"rsi": 71.0}

    def test_missing_signal_gives_empty_context(
        self, db_session: Session, instrument: Instrument
    ) -> None:
        quotes = FakeQuoteSource({instrument.id: Decimal("100")})
        broker = _broker(db_session, quotes)
        broker.place_order(_order(instrument, Side.BUY, 10, "co-open"))
        quotes.prices[instrument.id] = Decimal("110")
        broker.place_order(_order(instrument, Side.SELL, 10, "co-close"))

        [entry] = build_journal(db_session, Mode.PAPER)
        assert entry.entry_context == {}
        assert entry.exit_context == {}
