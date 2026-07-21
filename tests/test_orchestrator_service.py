"""Orchestrator integration (ROADMAP M11): candle -> strategy -> risk -> broker
end to end, driven by directly publishing `CandleReceived` events onto a real
`EventBus` — a "replayed session" (the same event shape a real live feed
produces, without needing an actual websocket connection), matching the
ROADMAP's own M11 testing plan. Also covers rejection handling and the
kill-switch circuit breaker on a handler that keeps failing.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import ClassVar

import pandas as pd
import pytest
from pydantic import BaseModel
from sqlalchemy.orm import Session, sessionmaker

from personaltrade.core.calendar import NSECalendar
from personaltrade.core.config import CostConfig, PaperConfig, RiskConfig
from personaltrade.core.enums import Interval, Mode, SignalDirection, SignalStatus
from personaltrade.core.events import CandleReceived, EventBus
from personaltrade.data.live.feed import LiveFeed
from personaltrade.data.providers.base import InstrumentInfo, Quote
from personaltrade.data.store.db import build_engine, build_session_factory, session_scope
from personaltrade.data.store.models import Base, Instrument
from personaltrade.data.store.repos import (
    InstrumentRepository,
    OrderRepository,
    PositionRepository,
    SignalRepository,
)
from personaltrade.orchestrator.runner import LiveStrategyRunner
from personaltrade.orchestrator.service import Orchestrator
from personaltrade.risk.kill_switch import KillSwitch
from personaltrade.risk.sizing import FixedFractionalSizer
from personaltrade.strategy.base import IndicatorSpec, Signal, Strategy, StrategyContext

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


class _Params(BaseModel):
    model_config = {"extra": "forbid"}


class _ScriptedLiveStrategy:
    """No indicators needed (warm from bar 1): LONG on the 1st call, EXIT on
    the 3rd, nothing on the 2nd."""

    name: ClassVar[str] = "scripted_live"
    params_schema: ClassVar[type[BaseModel]] = _Params

    def __init__(self, params: _Params | None = None) -> None:
        self.params = params or _Params()
        self.calls = 0

    def clone(self) -> _ScriptedLiveStrategy:
        return _ScriptedLiveStrategy(self.params)

    def warmup_bars(self) -> int:
        return 0

    def required_indicators(self) -> dict[str, IndicatorSpec]:
        return {}

    def on_candle(self, ctx: StrategyContext) -> Signal | None:
        self.calls += 1
        close = float(ctx.candles["close"].iloc[-1])
        if self.calls == 1:
            return Signal(SignalDirection.LONG, close)
        if self.calls == 3:
            return Signal(SignalDirection.EXIT, close)
        return None


class _AlwaysFailsStrategy:
    """Raises on every call — drives the orchestrator's kill-switch circuit breaker."""

    name: ClassVar[str] = "always_fails"
    params_schema: ClassVar[type[BaseModel]] = _Params

    def __init__(self, params: _Params | None = None) -> None:
        self.params = params or _Params()

    def clone(self) -> _AlwaysFailsStrategy:
        return _AlwaysFailsStrategy(self.params)

    def warmup_bars(self) -> int:
        return 0

    def required_indicators(self) -> dict[str, IndicatorSpec]:
        return {}

    def on_candle(self, ctx: StrategyContext) -> Signal | None:
        raise RuntimeError("boom")


class _NoOpProvider:
    """Satisfies MarketDataProvider structurally — these tests replay candles
    directly onto the bus, never through an actual stream_quotes() call."""

    def get_instruments(self, exchange: str = "NSE") -> list[InstrumentInfo]:
        raise NotImplementedError

    def get_historical_candles(
        self, instrument_key: str, interval: Interval, from_date: date, to_date: date
    ) -> pd.DataFrame:
        raise NotImplementedError

    async def stream_quotes(self, instrument_keys: list[str]) -> AsyncGenerator[Quote, None]:
        return
        yield  # pragma: no cover — unreachable; makes this an async generator


def _candle(key: str, ts: datetime, close: str) -> CandleReceived:
    return CandleReceived(
        instrument_key=key,
        interval=Interval.M1,
        ts=ts,
        open=Decimal(close),
        high=Decimal(close),
        low=Decimal(close),
        close=Decimal(close),
        volume=10,
    )


@pytest.fixture()
def factory(tmp_path: Path) -> sessionmaker[Session]:
    engine = build_engine(tmp_path / "orch.db")
    Base.metadata.create_all(engine)
    fac = build_session_factory(engine)
    return fac


@pytest.fixture()
def instrument(factory: sessionmaker[Session]) -> Instrument:
    with session_scope(factory) as session:
        inst = InstrumentRepository(session).add(
            Instrument(
                symbol="AAA", exchange="NSE", instrument_key="NSE_EQ|AAA", tick_size=Decimal("0.05")
            )
        )
    return inst


def _build_orchestrator(
    factory: sessionmaker[Session],
    instrument: Instrument,
    strategy: Strategy,
    *,
    max_open_positions: int = 5,
    max_daily_loss_pct: str = "50",
    max_consecutive_errors: int = 3,
) -> Orchestrator:
    bus = EventBus()
    calendar = NSECalendar(holidays=set())
    feed = LiveFeed(_NoOpProvider(), bus, calendar, {instrument.instrument_key: [Interval.M1]})
    risk_cfg = RiskConfig(
        capital=Decimal("100000"),
        risk_per_trade_pct=Decimal("10"),
        max_open_positions=max_open_positions,
        max_daily_loss_pct=Decimal(max_daily_loss_pct),
    )
    risk_cfg.kill_switch.max_consecutive_errors = max_consecutive_errors
    runners = {instrument.instrument_key: LiveStrategyRunner(instrument, strategy)}
    orchestrator = Orchestrator(
        factory,
        feed,
        bus,
        runners,
        mode=Mode.PAPER,
        risk_cfg=risk_cfg,
        sizer=FixedFractionalSizer(risk_cfg.risk_per_trade_pct),
        cost_rates=ZERO_COSTS,
        paper_cfg=PaperConfig(slippage_bps=Decimal("0"), segment="DELIVERY", latency_ms=0),
        initial_cash=risk_cfg.capital,
        strategy_name=strategy.name,
        strategy_params={},
    )
    orchestrator.start_strategy_run()
    return orchestrator


class TestReplayedSession:
    def test_long_then_exit_round_trip(
        self, factory: sessionmaker[Session], instrument: Instrument
    ) -> None:
        strategy = _ScriptedLiveStrategy()
        orchestrator = _build_orchestrator(factory, instrument, strategy)
        bus = orchestrator.bus

        bus.publish(
            _candle(instrument.instrument_key, datetime(2026, 1, 1, 9, 15, tzinfo=UTC), "100")
        )
        bus.publish(
            _candle(instrument.instrument_key, datetime(2026, 1, 1, 9, 16, tzinfo=UTC), "101")
        )
        bus.publish(
            _candle(instrument.instrument_key, datetime(2026, 1, 1, 9, 17, tzinfo=UTC), "105")
        )

        with session_scope(factory) as session:
            position = PositionRepository(session).get_for(instrument.id, Mode.PAPER)
            assert position is not None
            assert position.qty == 0
            assert position.realized_pnl == Decimal("500")  # (105-100)*100 shares, zero costs

            open_orders = OrderRepository(session).list_open(Mode.PAPER)
            assert open_orders == []

            signals = SignalRepository(session).list_all()
            assert [s.status for s in signals] == [SignalStatus.APPROVED, SignalStatus.APPROVED]
            assert all(s.strategy_run_id == orchestrator.strategy_run_id for s in signals)
            orders = OrderRepository(session).list_all()
            assert {o.signal_id for o in orders} == {s.id for s in signals}

    def test_rejection_does_not_place_an_order(
        self, factory: sessionmaker[Session], instrument: Instrument
    ) -> None:
        # max_open_positions=1, already met by a pre-existing open position on
        # a DIFFERENT instrument -> this instrument's LONG signal is rejected.
        with session_scope(factory) as session:
            other = InstrumentRepository(session).add(
                Instrument(
                    symbol="BBB",
                    exchange="NSE",
                    instrument_key="NSE_EQ|BBB",
                    tick_size=Decimal("0.05"),
                )
            )
            session.flush()
            PositionRepository(session).get_or_create(other.id, Mode.PAPER).qty = 5

        strategy = _ScriptedLiveStrategy()
        orchestrator = _build_orchestrator(factory, instrument, strategy, max_open_positions=1)
        orchestrator.bus.publish(
            _candle(instrument.instrument_key, datetime(2026, 1, 1, 9, 15, tzinfo=UTC), "100")
        )

        with session_scope(factory) as session:
            position = PositionRepository(session).get_for(instrument.id, Mode.PAPER)
            assert position is None or position.qty == 0
            assert OrderRepository(session).list_open(Mode.PAPER) == []

            signals = SignalRepository(session).list_all()
            assert len(signals) == 1
            assert signals[0].status == SignalStatus.REJECTED


class TestKillSwitchCircuitBreaker:
    def test_repeated_handler_failures_trip_the_kill_switch(
        self, factory: sessionmaker[Session], instrument: Instrument
    ) -> None:
        strategy = _AlwaysFailsStrategy()
        orchestrator = _build_orchestrator(factory, instrument, strategy, max_consecutive_errors=3)
        bus = orchestrator.bus

        for i in range(2):
            bus.publish(
                _candle(
                    instrument.instrument_key, datetime(2026, 1, 1, 9, 15 + i, tzinfo=UTC), "100"
                )
            )
        with session_scope(factory) as session:
            assert KillSwitch(session).is_tripped() is False

        bus.publish(
            _candle(instrument.instrument_key, datetime(2026, 1, 1, 9, 18, tzinfo=UTC), "100")
        )
        with session_scope(factory) as session:
            state = KillSwitch(session).state()
            assert state.tripped is True
            assert state.consecutive_errors == 3

    def test_a_success_after_failures_resets_the_counter(
        self, factory: sessionmaker[Session], instrument: Instrument
    ) -> None:
        failing = _AlwaysFailsStrategy()
        orchestrator = _build_orchestrator(factory, instrument, failing, max_consecutive_errors=3)
        bus = orchestrator.bus
        bus.publish(
            _candle(instrument.instrument_key, datetime(2026, 1, 1, 9, 15, tzinfo=UTC), "100")
        )
        bus.publish(
            _candle(instrument.instrument_key, datetime(2026, 1, 1, 9, 16, tzinfo=UTC), "100")
        )

        # swap in a strategy that succeeds (no signal at all) for this instrument
        orchestrator.runners[instrument.instrument_key] = LiveStrategyRunner(
            instrument, _ScriptedLiveStrategy()
        )
        # _ScriptedLiveStrategy emits LONG on its 1st call, which succeeds -> record_success()
        bus.publish(
            _candle(instrument.instrument_key, datetime(2026, 1, 1, 9, 17, tzinfo=UTC), "100")
        )

        with session_scope(factory) as session:
            state = KillSwitch(session).state()
            assert state.tripped is False
            assert state.consecutive_errors == 0
