from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from personaltrade.core.config import RiskConfig
from personaltrade.core.enums import Mode, RiskEventKind, Side, SignalDirection
from personaltrade.data.store.models import Instrument, Position
from personaltrade.data.store.repos import (
    InstrumentRepository,
    PositionRepository,
    RiskEventRepository,
)
from personaltrade.risk.engine import (
    ApprovedOrder,
    Rejection,
    RejectionReason,
    RiskEngine,
    _to_tick_decimal,
)
from personaltrade.risk.kill_switch import KillSwitch
from personaltrade.risk.sizing import FixedFractionalSizer
from personaltrade.strategy.base import Signal

EQUITY = Decimal("500000")


@pytest.fixture()
def instrument(db_session: Session) -> Instrument:
    inst = InstrumentRepository(db_session).add(
        Instrument(
            symbol="RELIANCE",
            exchange="NSE",
            instrument_key="NSE_EQ|RELIANCE",
            tick_size=Decimal("0.05"),
        )
    )
    db_session.flush()
    return inst


@pytest.fixture()
def engine(db_session: Session) -> RiskEngine:
    config = RiskConfig(
        capital=EQUITY,
        risk_per_trade_pct=Decimal("1.0"),
        max_open_positions=2,
        max_daily_loss_pct=Decimal("3.0"),
    )
    return RiskEngine(db_session, config, FixedFractionalSizer(config.risk_per_trade_pct))


def _evaluate(
    engine: RiskEngine,
    instrument: Instrument,
    signal: Signal,
    *,
    equity: Decimal = EQUITY,
    daily_realized_pnl: Decimal = Decimal("0"),
) -> ApprovedOrder | Rejection:
    return engine.evaluate(
        signal,
        instrument=instrument,
        mode=Mode.PAPER,
        equity=equity,
        daily_realized_pnl=daily_realized_pnl,
    )


def _open_position(
    db_session: Session, instrument: Instrument, qty: int, avg_price: str
) -> Position:
    position = PositionRepository(db_session).get_or_create(instrument.id, Mode.PAPER)
    position.qty = qty
    position.avg_price = Decimal(avg_price)
    db_session.flush()
    return position


class TestQuantizeToTick:
    def test_rounds_to_nearest_tick(self) -> None:
        assert _to_tick_decimal(1301.23, Decimal("0.05")) == Decimal("1301.25")

    def test_exact_multiple_unchanged(self) -> None:
        assert _to_tick_decimal(1300.00, Decimal("0.05")) == Decimal("1300.00")

    def test_zero_tick_size_returns_raw(self) -> None:
        assert _to_tick_decimal(1301.234, Decimal("0")) == Decimal("1301.234")


class TestOpeningLong:
    def test_approves_and_sizes(self, engine: RiskEngine, instrument: Instrument) -> None:
        result = _evaluate(engine, instrument, Signal(SignalDirection.LONG, ref_price=1000.0))
        assert isinstance(result, ApprovedOrder)
        assert result.side == Side.BUY
        # allocation = 500000 * 1% = 5000; price~1000 -> qty=5
        assert result.qty == 5
        assert result.limit_price is None

    def test_client_order_id_unique_per_call(
        self, engine: RiskEngine, instrument: Instrument
    ) -> None:
        r1 = _evaluate(engine, instrument, Signal(SignalDirection.LONG, ref_price=1000.0))
        r2 = _evaluate(engine, instrument, Signal(SignalDirection.LONG, ref_price=1000.0))
        assert isinstance(r1, ApprovedOrder)
        assert isinstance(r2, ApprovedOrder)
        assert r1.client_order_id != r2.client_order_id

    def test_approval_does_not_log_a_risk_event(
        self, db_session: Session, engine: RiskEngine, instrument: Instrument
    ) -> None:
        _evaluate(engine, instrument, Signal(SignalDirection.LONG, ref_price=1000.0))
        assert RiskEventRepository(db_session).list_all() == []

    def test_zero_quantity_rejected(self, engine: RiskEngine, instrument: Instrument) -> None:
        # price so high that 1% of equity can't buy even 1 share.
        result = _evaluate(engine, instrument, Signal(SignalDirection.LONG, ref_price=50_000_000.0))
        assert isinstance(result, Rejection)
        assert result.reason == RejectionReason.ZERO_QUANTITY


class TestOpeningShort:
    def test_approves_sell_side(self, engine: RiskEngine, instrument: Instrument) -> None:
        result = _evaluate(engine, instrument, Signal(SignalDirection.SHORT, ref_price=1000.0))
        assert isinstance(result, ApprovedOrder)
        assert result.side == Side.SELL
        assert result.qty == 5


class TestAlreadyInPosition:
    def test_long_signal_while_long_rejected(
        self, db_session: Session, engine: RiskEngine, instrument: Instrument
    ) -> None:
        _open_position(db_session, instrument, qty=10, avg_price="1000")
        result = _evaluate(engine, instrument, Signal(SignalDirection.LONG, ref_price=1010.0))
        assert isinstance(result, Rejection)
        assert result.reason == RejectionReason.ALREADY_IN_POSITION

    def test_short_signal_while_long_rejected_not_reversed(
        self, db_session: Session, engine: RiskEngine, instrument: Instrument
    ) -> None:
        # Mirrors ADR-015: reversal requires EXIT first, never a direct flip.
        _open_position(db_session, instrument, qty=10, avg_price="1000")
        result = _evaluate(engine, instrument, Signal(SignalDirection.SHORT, ref_price=990.0))
        assert isinstance(result, Rejection)
        assert result.reason == RejectionReason.ALREADY_IN_POSITION


class TestExit:
    def test_exit_while_flat_rejected(self, engine: RiskEngine, instrument: Instrument) -> None:
        result = _evaluate(engine, instrument, Signal(SignalDirection.EXIT, ref_price=1000.0))
        assert isinstance(result, Rejection)
        assert result.reason == RejectionReason.NO_OPEN_POSITION

    def test_exit_long_sells_full_qty(
        self, db_session: Session, engine: RiskEngine, instrument: Instrument
    ) -> None:
        _open_position(db_session, instrument, qty=10, avg_price="1000")
        result = _evaluate(engine, instrument, Signal(SignalDirection.EXIT, ref_price=1050.0))
        assert isinstance(result, ApprovedOrder)
        assert result.side == Side.SELL
        assert result.qty == 10

    def test_exit_short_buys_full_qty(
        self, db_session: Session, engine: RiskEngine, instrument: Instrument
    ) -> None:
        _open_position(db_session, instrument, qty=-7, avg_price="1000")
        result = _evaluate(engine, instrument, Signal(SignalDirection.EXIT, ref_price=950.0))
        assert isinstance(result, ApprovedOrder)
        assert result.side == Side.BUY
        assert result.qty == 7


class TestMaxOpenPositions:
    def test_breach_rejects_new_entries(
        self, db_session: Session, engine: RiskEngine, instrument: Instrument
    ) -> None:
        # config.max_open_positions == 2; seed 2 other open positions.
        for i in range(2):
            other = InstrumentRepository(db_session).add(
                Instrument(
                    symbol=f"SYM{i}",
                    exchange="NSE",
                    instrument_key=f"NSE_EQ|SYM{i}",
                    tick_size=Decimal("0.05"),
                )
            )
            db_session.flush()
            _open_position(db_session, other, qty=5, avg_price="500")

        result = _evaluate(engine, instrument, Signal(SignalDirection.LONG, ref_price=1000.0))
        assert isinstance(result, Rejection)
        assert result.reason == RejectionReason.MAX_OPEN_POSITIONS

    def test_breach_logs_limit_breach_event(
        self, db_session: Session, engine: RiskEngine, instrument: Instrument
    ) -> None:
        for i in range(2):
            other = InstrumentRepository(db_session).add(
                Instrument(
                    symbol=f"SYM{i}",
                    exchange="NSE",
                    instrument_key=f"NSE_EQ|SYM{i}",
                    tick_size=Decimal("0.05"),
                )
            )
            db_session.flush()
            _open_position(db_session, other, qty=5, avg_price="500")

        _evaluate(engine, instrument, Signal(SignalDirection.LONG, ref_price=1000.0))
        events = RiskEventRepository(db_session).list_all()
        assert len(events) == 1
        assert events[0].kind == RiskEventKind.LIMIT_BREACH

    def test_exit_still_allowed_when_at_cap(
        self, db_session: Session, engine: RiskEngine, instrument: Instrument
    ) -> None:
        """The cap gates new entries, never an exit — an exit reduces exposure."""
        _open_position(db_session, instrument, qty=10, avg_price="1000")
        for i in range(2):
            other = InstrumentRepository(db_session).add(
                Instrument(
                    symbol=f"SYM{i}",
                    exchange="NSE",
                    instrument_key=f"NSE_EQ|SYM{i}",
                    tick_size=Decimal("0.05"),
                )
            )
            db_session.flush()
            _open_position(db_session, other, qty=5, avg_price="500")

        result = _evaluate(engine, instrument, Signal(SignalDirection.EXIT, ref_price=1050.0))
        assert isinstance(result, ApprovedOrder)


class TestMaxDailyLoss:
    def test_breach_rejects_everything(self, engine: RiskEngine, instrument: Instrument) -> None:
        result = _evaluate(
            engine,
            instrument,
            Signal(SignalDirection.LONG, ref_price=1000.0),
            daily_realized_pnl=Decimal("-20000"),  # -4% > 3% cap
        )
        assert isinstance(result, Rejection)
        assert result.reason == RejectionReason.MAX_DAILY_LOSS

    def test_breach_takes_priority_over_exit(
        self, db_session: Session, engine: RiskEngine, instrument: Instrument
    ) -> None:
        """Even an EXIT is blocked once the daily-loss circuit trips — the whole
        point is to stop trading, not just stop opening new risk. A human (kill
        switch reset, or the M9+ orchestrator) decides what happens to open
        positions from here, not the risk engine silently keeps trading."""
        _open_position(db_session, instrument, qty=10, avg_price="1000")
        result = _evaluate(
            engine,
            instrument,
            Signal(SignalDirection.EXIT, ref_price=900.0),
            daily_realized_pnl=Decimal("-20000"),
        )
        assert isinstance(result, Rejection)
        assert result.reason == RejectionReason.MAX_DAILY_LOSS

    def test_breach_logs_limit_breach_event(
        self, db_session: Session, engine: RiskEngine, instrument: Instrument
    ) -> None:
        _evaluate(
            engine,
            instrument,
            Signal(SignalDirection.LONG, ref_price=1000.0),
            daily_realized_pnl=Decimal("-20000"),
        )
        events = RiskEventRepository(db_session).list_all()
        assert len(events) == 1
        assert events[0].kind == RiskEventKind.LIMIT_BREACH


class TestKillSwitchGate:
    def test_tripped_rejects_everything_first(
        self, db_session: Session, engine: RiskEngine, instrument: Instrument
    ) -> None:
        KillSwitch(db_session).trip("manual halt")
        result = _evaluate(engine, instrument, Signal(SignalDirection.LONG, ref_price=1000.0))
        assert isinstance(result, Rejection)
        assert result.reason == RejectionReason.KILL_SWITCH_TRIPPED

    def test_tripped_blocks_exit_too(
        self, db_session: Session, engine: RiskEngine, instrument: Instrument
    ) -> None:
        _open_position(db_session, instrument, qty=10, avg_price="1000")
        KillSwitch(db_session).trip("manual halt")
        result = _evaluate(engine, instrument, Signal(SignalDirection.EXIT, ref_price=900.0))
        assert isinstance(result, Rejection)
        assert result.reason == RejectionReason.KILL_SWITCH_TRIPPED

    def test_reset_restores_normal_evaluation(
        self, db_session: Session, engine: RiskEngine, instrument: Instrument
    ) -> None:
        ks = KillSwitch(db_session)
        ks.trip("manual halt")
        ks.reset("reviewed")
        result = _evaluate(engine, instrument, Signal(SignalDirection.LONG, ref_price=1000.0))
        assert isinstance(result, ApprovedOrder)
