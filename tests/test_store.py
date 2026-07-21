from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import Session

from personaltrade.core.enums import (
    Mode,
    OrderState,
    OrderType,
    Side,
    SignalDirection,
)
from personaltrade.data.store.models import (
    Instrument,
    NewsItem,
    Order,
    Position,
    Signal,
    StrategyRun,
    Trade,
)
from personaltrade.data.store.repos import (
    InstrumentRepository,
    InvalidOrderTransition,
    NewsRepository,
    OrderRepository,
    PositionRepository,
)


def _instrument(session: Session, symbol: str = "RELIANCE") -> Instrument:
    repo = InstrumentRepository(session)
    return repo.add(
        Instrument(
            symbol=symbol,
            exchange="NSE",
            instrument_key=f"NSE_EQ|{symbol}",
            tick_size=Decimal("0.05"),
        )
    )


def _order(session: Session, instrument: Instrument, client_order_id: str = "co-1") -> Order:
    run = StrategyRun(strategy_name="ema_cross", mode=Mode.PAPER)
    session.add(run)
    session.flush()
    signal = Signal(
        instrument_id=instrument.id,
        strategy_run_id=run.id,
        direction=SignalDirection.LONG,
        ref_price=Decimal("2850.55"),
    )
    session.add(signal)
    session.flush()
    return OrderRepository(session).record_created(
        Order(
            client_order_id=client_order_id,
            instrument_id=instrument.id,
            signal_id=signal.id,
            side=Side.BUY,
            order_type=OrderType.LIMIT,
            qty=10,
            limit_price=Decimal("2850.55"),
            mode=Mode.PAPER,
        )
    )


class TestMoneyAndTime:
    def test_decimal_roundtrip_exact(self, db_session: Session) -> None:
        ins = _instrument(db_session)
        order = _order(db_session, ins)
        trade = Trade(
            order_id=order.id,
            price=Decimal("2850.55"),
            qty=10,
            stt=Decimal("0.03"),
            net_amount=Decimal("28505.53"),
        )
        db_session.add(trade)
        db_session.commit()
        db_session.expire_all()

        loaded = db_session.get(Trade, trade.id)
        assert loaded is not None
        assert loaded.price == Decimal("2850.55")
        assert isinstance(loaded.price, Decimal)
        assert loaded.stt == Decimal("0.03")

    def test_float_money_rejected(self, db_session: Session) -> None:
        ins = _instrument(db_session)
        order = _order(db_session, ins)
        db_session.add(Trade(order_id=order.id, price=2850.55, qty=10, net_amount=Decimal("1")))
        with pytest.raises(StatementError, match="Decimal, not float"):
            db_session.flush()

    def test_naive_datetime_rejected(self, db_session: Session) -> None:
        db_session.add(
            NewsItem(
                source="x",
                url="https://ex.com/1",
                title="t",
                published_at=datetime(2026, 7, 19, 10, 0),  # naive
            )
        )
        with pytest.raises(StatementError, match="naive datetime"):
            db_session.flush()

    def test_datetimes_come_back_utc_aware(self, db_session: Session) -> None:
        ins = _instrument(db_session)
        db_session.commit()
        db_session.expire_all()
        loaded = db_session.get(Instrument, ins.id)
        assert loaded is not None
        order = _order(db_session, loaded)
        db_session.commit()
        db_session.expire_all()
        reloaded = db_session.get(Order, order.id)
        assert reloaded is not None
        assert reloaded.created_at.tzinfo == UTC


class TestInstrumentRepository:
    def test_get_by_symbol(self, db_session: Session) -> None:
        _instrument(db_session, "INFY")
        repo = InstrumentRepository(db_session)
        found = repo.get_by_symbol("INFY")
        assert found is not None
        assert found.instrument_key == "NSE_EQ|INFY"
        assert repo.get_by_symbol("NOPE") is None

    def test_symbol_exchange_unique(self, db_session: Session) -> None:
        _instrument(db_session, "INFY")
        db_session.add(
            Instrument(
                symbol="INFY",
                exchange="NSE",
                instrument_key="NSE_EQ|INFY-DUP",
                tick_size=Decimal("0.05"),
            )
        )
        with pytest.raises(IntegrityError):
            db_session.flush()

    def test_list_active_excludes_inactive(self, db_session: Session) -> None:
        active = _instrument(db_session, "INFY")
        inactive = _instrument(db_session, "DELISTED")
        inactive.active = False
        db_session.flush()

        result = InstrumentRepository(db_session).list_active()
        assert active in result
        assert inactive not in result


class TestOrderLifecycle:
    def test_happy_path_records_full_audit_trail(self, db_session: Session) -> None:
        ins = _instrument(db_session)
        order = _order(db_session, ins)
        repo = OrderRepository(db_session)

        assert order.state == OrderState.PENDING_RISK
        repo.transition(order, OrderState.SUBMITTING)
        repo.transition(order, OrderState.SUBMITTED, {"broker_order_id": "UPX-1"})
        repo.transition(order, OrderState.OPEN)
        repo.transition(order, OrderState.PARTIALLY_FILLED, {"filled": 4})
        repo.transition(order, OrderState.PARTIALLY_FILLED, {"filled": 8})
        repo.transition(order, OrderState.FILLED, {"filled": 10})
        db_session.commit()

        chain = [(e.from_state, e.to_state) for e in order.events]
        assert chain == [
            (None, OrderState.PENDING_RISK),
            (OrderState.PENDING_RISK, OrderState.SUBMITTING),
            (OrderState.SUBMITTING, OrderState.SUBMITTED),
            (OrderState.SUBMITTED, OrderState.OPEN),
            (OrderState.OPEN, OrderState.PARTIALLY_FILLED),
            (OrderState.PARTIALLY_FILLED, OrderState.PARTIALLY_FILLED),
            (OrderState.PARTIALLY_FILLED, OrderState.FILLED),
        ]
        assert order.events[2].payload == {"broker_order_id": "UPX-1"}

    def test_illegal_transition_rejected(self, db_session: Session) -> None:
        ins = _instrument(db_session)
        order = _order(db_session, ins)
        repo = OrderRepository(db_session)
        with pytest.raises(InvalidOrderTransition):
            repo.transition(order, OrderState.FILLED)  # PENDING_RISK -> FILLED is illegal

    def test_terminal_state_is_final(self, db_session: Session) -> None:
        ins = _instrument(db_session)
        order = _order(db_session, ins)
        repo = OrderRepository(db_session)
        repo.transition(order, OrderState.REJECTED_RISK)
        with pytest.raises(InvalidOrderTransition):
            repo.transition(order, OrderState.SUBMITTING)

    def test_get_by_client_order_id(self, db_session: Session) -> None:
        ins = _instrument(db_session)
        _order(db_session, ins, client_order_id="co-42")
        found = OrderRepository(db_session).get_by_client_order_id("co-42")
        assert found is not None
        assert found.qty == 10

    def test_list_open_filters_by_state_and_mode(self, db_session: Session) -> None:
        ins = _instrument(db_session)
        o1 = _order(db_session, ins, client_order_id="co-a")
        o2 = _order(db_session, ins, client_order_id="co-b")
        repo = OrderRepository(db_session)
        repo.transition(o1, OrderState.SUBMITTING)
        repo.transition(o2, OrderState.REJECTED_RISK)
        open_orders = repo.list_open(Mode.PAPER)
        assert [o.client_order_id for o in open_orders] == ["co-a"]
        assert repo.list_open(Mode.LIVE) == []


class TestPositionRepository:
    def test_get_or_create_idempotent(self, db_session: Session) -> None:
        ins = _instrument(db_session)
        repo = PositionRepository(db_session)
        p1 = repo.get_or_create(ins.id, Mode.PAPER)
        p2 = repo.get_or_create(ins.id, Mode.PAPER)
        assert p1.id == p2.id
        assert p1.qty == 0

    def test_instrument_mode_unique(self, db_session: Session) -> None:
        ins = _instrument(db_session)
        db_session.add(Position(instrument_id=ins.id, mode=Mode.PAPER))
        db_session.add(Position(instrument_id=ins.id, mode=Mode.PAPER))
        with pytest.raises(IntegrityError):
            db_session.flush()


class TestNewsRepository:
    def test_dedup_by_url(self, db_session: Session) -> None:
        repo = NewsRepository(db_session)
        first = repo.add_if_new(NewsItem(source="rss", url="https://ex.com/a", title="A"))
        dup = repo.add_if_new(NewsItem(source="other", url="https://ex.com/a", title="A again"))
        assert first is not None
        assert dup is None
        assert len(repo.list_all()) == 1

    def test_list_for_instrument_filters_by_tag_and_since_newest_first(
        self, db_session: Session
    ) -> None:
        from personaltrade.data.store.models import NewsInstrumentTag

        inst = _instrument(db_session, "RELIANCE")
        other = _instrument(db_session, "TCS")
        repo = NewsRepository(db_session)

        older = repo.add_if_new(
            NewsItem(
                source="rss",
                url="https://ex.com/1",
                title="Older",
                published_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
        )
        newer = repo.add_if_new(
            NewsItem(
                source="rss",
                url="https://ex.com/2",
                title="Newer",
                published_at=datetime(2026, 6, 1, tzinfo=UTC),
            )
        )
        unrelated = repo.add_if_new(
            NewsItem(
                source="rss",
                url="https://ex.com/3",
                title="About TCS",
                published_at=datetime(2026, 6, 1, tzinfo=UTC),
            )
        )
        assert older is not None and newer is not None and unrelated is not None
        db_session.add(NewsInstrumentTag(news_item_id=older.id, instrument_id=inst.id))
        db_session.add(NewsInstrumentTag(news_item_id=newer.id, instrument_id=inst.id))
        db_session.add(NewsInstrumentTag(news_item_id=unrelated.id, instrument_id=other.id))
        db_session.flush()

        result = repo.list_for_instrument(inst.id, since=datetime(2026, 3, 1, tzinfo=UTC))
        assert [n.title for n in result] == ["Newer"]  # older excluded by since, unrelated by tag

    def test_list_for_instrument_falls_back_to_ingested_at_when_published_at_is_null(
        self, db_session: Session
    ) -> None:
        from personaltrade.data.store.models import NewsInstrumentTag

        inst = _instrument(db_session, "RELIANCE")
        repo = NewsRepository(db_session)
        item = repo.add_if_new(
            NewsItem(
                source="rss", url="https://ex.com/no-date", title="No pubDate", published_at=None
            )
        )
        assert item is not None
        db_session.add(NewsInstrumentTag(news_item_id=item.id, instrument_id=inst.id))
        db_session.flush()

        # ingested_at defaults to "now" (utcnow) — a since far in the past still finds it,
        # a since far in the future correctly excludes it.
        assert repo.list_for_instrument(inst.id, since=datetime(2000, 1, 1, tzinfo=UTC)) == [item]
        assert repo.list_for_instrument(inst.id, since=datetime(2100, 1, 1, tzinfo=UTC)) == []
