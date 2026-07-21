"""analytics/reports.py: per-instrument/per-strategy breakdown grouping, the
unattributed-trades fallback for pre-M12 orders with no linked Signal, and
generate_report's end-to-end wiring of pnl+journal (ROADMAP M12 testing plan).
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from personaltrade.analytics.reports import _UNATTRIBUTED, generate_report
from personaltrade.core.config import CostConfig, PaperConfig
from personaltrade.core.enums import (
    Interval,
    Mode,
    OrderType,
    Side,
    SignalDirection,
    SignalStatus,
)
from personaltrade.data.store.candles import CandleStore
from personaltrade.data.store.models import Instrument, Signal, StrategyRun
from personaltrade.data.store.repos import (
    InstrumentRepository,
    OrderRepository,
    SignalRepository,
    StrategyRunRepository,
)
from personaltrade.execution.broker import OrderRequest
from personaltrade.execution.paper.broker import PaperBroker
from tests.factories import FakeQuoteSource, ManualClock, synthetic_candles

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
def instruments(db_session: Session) -> tuple[Instrument, Instrument]:
    repo = InstrumentRepository(db_session)
    aaa = repo.add(
        Instrument(
            symbol="AAA", exchange="NSE", instrument_key="NSE_EQ|AAA", tick_size=Decimal("0.05")
        )
    )
    bbb = repo.add(
        Instrument(
            symbol="BBB", exchange="NSE", instrument_key="NSE_EQ|BBB", tick_size=Decimal("0.05")
        )
    )
    db_session.flush()
    return aaa, bbb


def _broker(
    session: Session, quotes: FakeQuoteSource, clock: ManualClock, cash: str = "1000000"
) -> PaperBroker:
    return PaperBroker(
        session,
        quotes,
        cost_rates=ZERO_COSTS,
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
    session: Session, client_order_id: str, instrument: Instrument, strategy_name: str
) -> None:
    run = StrategyRunRepository(session).add(
        StrategyRun(strategy_name=strategy_name, params={}, mode=Mode.PAPER)
    )
    session.flush()
    signal = SignalRepository(session).add(
        Signal(
            instrument_id=instrument.id,
            strategy_run_id=run.id,
            direction=SignalDirection.LONG,
            ref_price=Decimal("100"),
            context={},
            status=SignalStatus.APPROVED,
        )
    )
    session.flush()
    order = OrderRepository(session).get_by_client_order_id(client_order_id)
    assert order is not None
    order.signal_id = signal.id
    session.flush()


def _round_trip(
    session: Session,
    broker: PaperBroker,
    quotes: FakeQuoteSource,
    instrument: Instrument,
    clock: ManualClock,
    *,
    entry_price: Decimal,
    exit_price: Decimal,
    qty: int,
    tag: str,
    strategy_name: str | None = None,
) -> None:
    quotes.prices[instrument.id] = entry_price
    broker.place_order(_order(instrument, Side.BUY, qty, f"co-{tag}-open"))
    if strategy_name is not None:
        _attach_signal(session, f"co-{tag}-open", instrument, strategy_name)
    clock.advance(hours=1)
    quotes.prices[instrument.id] = exit_price
    broker.place_order(_order(instrument, Side.SELL, qty, f"co-{tag}-close"))
    if strategy_name is not None:
        _attach_signal(session, f"co-{tag}-close", instrument, strategy_name)
    clock.advance(hours=1)


class TestBreakdowns:
    def test_by_instrument_and_by_strategy(
        self, db_session: Session, instruments: tuple[Instrument, Instrument], tmp_path: Path
    ) -> None:
        aaa, bbb = instruments
        clock = ManualClock()
        quotes = FakeQuoteSource()
        broker = _broker(db_session, quotes, clock)
        since = clock.now()

        # AAA: strategy "trend", wins +100
        _round_trip(
            db_session,
            broker,
            quotes,
            aaa,
            clock,
            entry_price=Decimal("100"),
            exit_price=Decimal("110"),
            qty=10,
            tag="aaa",
            strategy_name="trend",
        )
        # BBB: strategy "meanrev", loses -50
        _round_trip(
            db_session,
            broker,
            quotes,
            bbb,
            clock,
            entry_price=Decimal("100"),
            exit_price=Decimal("95"),
            qty=10,
            tag="bbb",
            strategy_name="meanrev",
        )

        store = CandleStore(tmp_path / "candles")
        report = generate_report(
            db_session,
            store,
            mode=Mode.PAPER,
            initial_cash=Decimal("1000000"),
            interval=Interval.D1,
            since=since,
        )

        by_instrument = {b.label: b for b in report.by_instrument}
        assert by_instrument["AAA"].realized_pnl == Decimal("100")
        assert by_instrument["BBB"].realized_pnl == Decimal("-50")
        # sorted descending by realized_pnl
        assert [b.label for b in report.by_instrument] == ["AAA", "BBB"]

        by_strategy = {b.label: b for b in report.by_strategy}
        assert by_strategy["trend"].realized_pnl == Decimal("100")
        assert by_strategy["meanrev"].realized_pnl == Decimal("-50")

        assert report.summary.realized_pnl == Decimal("50")
        assert report.summary.closed_trades == 2
        assert len(report.journal) == 2

    def test_unattributed_trades_fallback(
        self, db_session: Session, instruments: tuple[Instrument, Instrument], tmp_path: Path
    ) -> None:
        aaa, _bbb = instruments
        clock = ManualClock()
        quotes = FakeQuoteSource()
        broker = _broker(db_session, quotes, clock)
        since = clock.now()

        _round_trip(
            db_session,
            broker,
            quotes,
            aaa,
            clock,
            entry_price=Decimal("100"),
            exit_price=Decimal("110"),
            qty=10,
            tag="aaa",  # no strategy_name -> no Signal attached
        )

        store = CandleStore(tmp_path / "candles")
        report = generate_report(
            db_session,
            store,
            mode=Mode.PAPER,
            initial_cash=Decimal("1000000"),
            interval=Interval.D1,
            since=since,
        )
        assert [b.label for b in report.by_strategy] == [_UNATTRIBUTED]


class TestGenerateReportSinceAndUnrealized:
    def test_since_excludes_earlier_realized_trades_but_not_unrealized(
        self, db_session: Session, instruments: tuple[Instrument, Instrument], tmp_path: Path
    ) -> None:
        aaa, bbb = instruments
        clock = ManualClock()
        quotes = FakeQuoteSource()
        broker = _broker(db_session, quotes, clock)

        # Realized round trip entirely BEFORE the cutoff.
        _round_trip(
            db_session,
            broker,
            quotes,
            aaa,
            clock,
            entry_price=Decimal("100"),
            exit_price=Decimal("110"),
            qty=10,
            tag="aaa",
        )

        since = clock.now()

        # An open position (never closed) on BBB, still contributes to unrealized.
        quotes.prices[bbb.id] = Decimal("100")
        broker.place_order(_order(bbb, Side.BUY, 5, "co-bbb-open"))

        store = CandleStore(tmp_path / "candles")
        store.write("BBB", "NSE", Interval.D1, synthetic_candles([100, 105, 118]))
        # synthetic close = open + 1 -> last close = 119

        report = generate_report(
            db_session,
            store,
            mode=Mode.PAPER,
            initial_cash=Decimal("1000000"),
            interval=Interval.D1,
            since=since,
        )

        assert report.summary.closed_trades == 0  # AAA round trip is before `since`
        assert report.by_instrument == []
        assert report.journal == []
        assert report.summary.unrealized_pnl == (Decimal("119") - Decimal("100")) * 5
