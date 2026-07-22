"""Full-session E2E + chaos tests (ROADMAP M18, ADR-028).

M9-M17 each unit-tested their own seam (paper broker fills, feed reconnect,
reconciliation functions, kill-switch circuit breaker). This file drives the
whole spine — LiveFeed -> EventBus -> Orchestrator -> RiskEngine -> Broker ->
persistence — together, across the three chaos scenarios ROADMAP M18 names:

- `TestMultiDaySessionReplay`: a continuous multi-day paper session in one
  process, proving no state corruption across day boundaries.
- `TestProcessRestartMidSession`: a genuine second `Orchestrator` (fresh
  strategy instance, fresh `StrategyRun`, same database) picking up exactly
  where a "killed" first one left off — including the risk engine correctly
  rejecting a naive re-entry attempt from a strategy that has no memory of
  the position it already holds (ADR-018's `ALREADY_IN_POSITION`).
- `TestFeedStalenessRecovery`: a real `LiveFeed` (tick -> bar aggregation,
  not bus.publish shortcuts) going quiet mid-session and recovering.
- `TestTokenExpiryMidSession`: every Upstox call failing with a stale-token
  401, proving the kill switch's circuit breaker trips instead of crashing
  the process or leaving partial state (ADR-021's per-candle transaction
  rolls the whole attempt back).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import ClassVar

import httpx
import pandas as pd
import pytest
from pydantic import BaseModel
from sqlalchemy.orm import Session, sessionmaker

from personaltrade.core.calendar import NSECalendar
from personaltrade.core.config import CostConfig, PaperConfig, RiskConfig, UpstoxConfig
from personaltrade.core.enums import (
    Interval,
    Mode,
    OrderState,
    SignalDirection,
    SignalStatus,
)
from personaltrade.core.events import CandleReceived, EventBus, FeedStale
from personaltrade.data.live.feed import LiveFeed
from personaltrade.data.providers.base import InstrumentInfo, Quote
from personaltrade.data.store.db import build_engine, build_session_factory, session_scope
from personaltrade.data.store.models import Base, Instrument
from personaltrade.data.store.repos import (
    InstrumentRepository,
    OrderRepository,
    PositionRepository,
    SignalRepository,
    StrategyRunRepository,
)
from personaltrade.orchestrator.runner import LiveStrategyRunner
from personaltrade.orchestrator.service import Orchestrator
from personaltrade.risk.kill_switch import KillSwitch
from personaltrade.risk.sizing import FixedFractionalSizer
from personaltrade.strategy.base import IndicatorSpec, Signal, Strategy, StrategyContext
from tests.factories import LeakyOnceStrategy, ManualClock

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
    the 3rd, nothing on the 2nd — same schedule as
    test_orchestrator_service.py's version, duplicated locally (each
    orchestrator test file owns its own tiny strategy doubles rather than
    sharing across test modules, this codebase's established convention)."""

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


class _AlwaysLongStrategy:
    """Emits LONG every call, oblivious to whether it's already in a
    position — the broker/risk layer, not the strategy, is what must stay
    safe under repeated failures (TestTokenExpiryMidSession)."""

    name: ClassVar[str] = "always_long"
    params_schema: ClassVar[type[BaseModel]] = _Params

    def __init__(self, params: _Params | None = None) -> None:
        self.params = params or _Params()

    def clone(self) -> _AlwaysLongStrategy:
        return _AlwaysLongStrategy(self.params)

    def warmup_bars(self) -> int:
        return 0

    def required_indicators(self) -> dict[str, IndicatorSpec]:
        return {}

    def on_candle(self, ctx: StrategyContext) -> Signal | None:
        close = float(ctx.candles["close"].iloc[-1])
        return Signal(SignalDirection.LONG, close)


class _NoOpProvider:
    """Satisfies MarketDataProvider structurally — most tests here replay
    candles/ticks directly, never through an actual stream_quotes() call."""

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


def _quote(key: str, ltp: str, ltt: datetime) -> Quote:
    return Quote(instrument_key=key, ltp=Decimal(ltp), ltq=10, ltt=ltt, close=Decimal(ltp))


@pytest.fixture()
def factory(tmp_path: Path) -> sessionmaker[Session]:
    engine = build_engine(tmp_path / "e2e.db")
    Base.metadata.create_all(engine)
    return build_session_factory(engine)


@pytest.fixture()
def instrument(factory: sessionmaker[Session]) -> Instrument:
    with session_scope(factory) as session:
        inst = InstrumentRepository(session).add(
            Instrument(
                symbol="AAA", exchange="NSE", instrument_key="NSE_EQ|AAA", tick_size=Decimal("0.05")
            )
        )
    return inst


def _risk_cfg(max_consecutive_errors: int = 3) -> RiskConfig:
    cfg = RiskConfig(
        capital=Decimal("100000"),
        risk_per_trade_pct=Decimal("10"),
        max_open_positions=5,
        max_daily_loss_pct=Decimal("50"),
    )
    cfg.kill_switch.max_consecutive_errors = max_consecutive_errors
    return cfg


def _paper_cfg() -> PaperConfig:
    return PaperConfig(slippage_bps=Decimal("0"), segment="DELIVERY", latency_ms=0)


def _build_paper_orchestrator(
    factory: sessionmaker[Session],
    instrument: Instrument,
    strategy: Strategy,
    *,
    bus: EventBus | None = None,
    feed: LiveFeed | None = None,
    clock: ManualClock | None = None,
) -> Orchestrator:
    bus = bus or EventBus()
    calendar = NSECalendar(holidays=set())
    feed = feed or LiveFeed(
        _NoOpProvider(), bus, calendar, {instrument.instrument_key: [Interval.M1]}
    )
    risk_cfg = _risk_cfg()
    orchestrator = Orchestrator(
        factory,
        feed,
        bus,
        {instrument.instrument_key: LiveStrategyRunner(instrument, strategy)},
        mode=Mode.PAPER,
        risk_cfg=risk_cfg,
        sizer=FixedFractionalSizer(risk_cfg.risk_per_trade_pct),
        cost_rates=ZERO_COSTS,
        paper_cfg=_paper_cfg(),
        initial_cash=risk_cfg.capital,
        strategy_name=strategy.name,
        strategy_params={},
        clock=clock,
    )
    orchestrator.start_strategy_run()
    return orchestrator


class TestMultiDaySessionReplay:
    """A continuous 5-"day" paper session (LONG/EXIT round trips back to back)
    in one process — the honest baseline every chaos test below perturbs."""

    def test_five_day_round_trip_cycle_stays_consistent(
        self, factory: sessionmaker[Session], instrument: Instrument
    ) -> None:
        strategy = _ScriptedLiveStrategy()
        # 5 independent LONG->flat->EXIT cycles: reset the strategy's own
        # call counter between "days" the way a fresh session naturally would
        # (a new day's first bar is call #1 again), while the DATABASE state
        # (position/orders/kill-switch) carries forward continuously.
        orchestrator = _build_paper_orchestrator(factory, instrument, strategy)
        bus = orchestrator.bus

        day0 = datetime(2026, 1, 1, 9, 15, tzinfo=UTC)
        for day in range(5):
            base = day0 + timedelta(days=day)
            strategy.calls = 0  # new trading day, fresh strategy call count
            bus.publish(_candle(instrument.instrument_key, base, "100"))
            bus.publish(_candle(instrument.instrument_key, base + timedelta(minutes=1), "101"))
            bus.publish(_candle(instrument.instrument_key, base + timedelta(minutes=2), "105"))

        with session_scope(factory) as session:
            position = PositionRepository(session).get_for(instrument.id, Mode.PAPER)
            assert position is not None
            assert position.qty == 0  # flat at the end of every cycle
            # >= 5 x (105-100)*100 shares: each day's qty is sized off that
            # day's (compounding) equity, so later days trade slightly larger
            # size than the first — the exact total depends on that
            # compounding, but it must be strictly positive and at least the
            # non-compounding baseline.
            assert position.realized_pnl >= Decimal("2500")

            signals = SignalRepository(session).list_all()
            assert len(signals) == 10  # 2 approved signals x 5 days
            assert all(s.status == SignalStatus.APPROVED for s in signals)

            orders = OrderRepository(session).list_all()
            assert len(orders) == 10
            assert all(o.state == OrderState.FILLED for o in orders)

            assert KillSwitch(session).is_tripped() is False


class TestProcessRestartMidSession:
    """A "kill -9" between opening and closing a position: a fresh
    `Orchestrator` (fresh strategy instance, fresh `StrategyRun` row) against
    the same database must continue correctly, and the risk engine — not the
    strategy, which has no memory of the position it already holds — must
    reject a naive duplicate entry attempt."""

    def test_position_and_continuity_survive_a_restart(
        self, factory: sessionmaker[Session], instrument: Instrument
    ) -> None:
        strategy1 = _ScriptedLiveStrategy()
        orchestrator1 = _build_paper_orchestrator(factory, instrument, strategy1)
        run1_id = orchestrator1.strategy_run_id

        orchestrator1.bus.publish(
            _candle(instrument.instrument_key, datetime(2026, 1, 1, 9, 15, tzinfo=UTC), "100")
        )  # strategy1's call #1 -> LONG -> position opens at avg_price=100

        with session_scope(factory) as session:
            position = PositionRepository(session).get_for(instrument.id, Mode.PAPER)
            assert position is not None
            assert position.qty == 100
            assert position.avg_price == Decimal("100")

        # --- "crash": orchestrator1 is simply abandoned, never touched again ---

        strategy2 = _ScriptedLiveStrategy()  # fresh instance -> its own call count restarts at 0
        orchestrator2 = _build_paper_orchestrator(factory, instrument, strategy2)
        run2_id = orchestrator2.strategy_run_id
        assert run2_id != run1_id

        findings = orchestrator2.reconcile()
        assert findings == []  # orchestrator1's one-transaction-per-candle committed cleanly

        # strategy2's call #1 naively re-emits LONG, unaware a position already
        # exists -- the risk engine (reading the persisted Position, not
        # process memory) must reject it, not double the position.
        orchestrator2.bus.publish(
            _candle(instrument.instrument_key, datetime(2026, 1, 1, 9, 16, tzinfo=UTC), "102")
        )
        with session_scope(factory) as session:
            position = PositionRepository(session).get_for(instrument.id, Mode.PAPER)
            assert position is not None
            assert position.qty == 100  # unchanged -- no duplicate entry
            signals = SignalRepository(session).list_all()
            assert signals[-1].status == SignalStatus.REJECTED

        orchestrator2.bus.publish(  # strategy2's call #2 -> no-op, per its schedule
            _candle(instrument.instrument_key, datetime(2026, 1, 1, 9, 17, tzinfo=UTC), "103")
        )
        orchestrator2.bus.publish(  # strategy2's call #3 -> EXIT
            _candle(instrument.instrument_key, datetime(2026, 1, 1, 9, 18, tzinfo=UTC), "105")
        )

        with session_scope(factory) as session:
            position = PositionRepository(session).get_for(instrument.id, Mode.PAPER)
            assert position is not None
            assert position.qty == 0
            # (105-100)*100: realized against the ORIGINAL entry price from
            # the pre-restart process, proving continuity, not a fresh basis.
            assert position.realized_pnl == Decimal("500")

            orders = OrderRepository(session).list_all()
            assert len(orders) == 2  # one open (pre-restart) + one close (post-restart), no orphans
            assert {o.state for o in orders} == {OrderState.FILLED}

            runs = StrategyRunRepository(session).list_all()
            assert len(runs) == 2  # the restart legitimately started a new StrategyRun


class TestFeedStalenessRecovery:
    """A real `LiveFeed` (genuine tick -> bar aggregation, not a bus.publish
    shortcut) going quiet mid-session: `FeedStale` fires exactly once, the
    orchestrator logs it without raising, and candles resume being processed
    normally once ticks return."""

    def test_stale_feed_detected_then_recovers_without_losing_candles(
        self, factory: sessionmaker[Session], instrument: Instrument
    ) -> None:
        clock = ManualClock(datetime(2026, 1, 1, 9, 15, tzinfo=UTC))
        bus = EventBus()
        calendar = NSECalendar(holidays=set())
        feed = LiveFeed(
            _NoOpProvider(),
            bus,
            calendar,
            {instrument.instrument_key: [Interval.M1]},
            staleness_threshold=timedelta(seconds=30),
            clock=clock,
        )
        stale_events: list[FeedStale] = []
        bus.subscribe(FeedStale, stale_events.append)

        strategy = LeakyOnceStrategy()  # emits LONG on its very first call, nothing after
        orchestrator = _build_paper_orchestrator(
            factory, instrument, strategy, bus=bus, feed=feed, clock=clock
        )

        # ticks flow normally -> a completed 1m bar -> real CandleReceived -> LONG
        feed.on_tick(_quote(instrument.instrument_key, "100", clock.now()))
        clock.advance(minutes=1, seconds=5)
        feed.on_tick(_quote(instrument.instrument_key, "101", clock.now()))

        with session_scope(factory) as session:
            position = PositionRepository(session).get_for(instrument.id, Mode.PAPER)
            assert position is not None
            assert position.qty == 100

        # the feed goes quiet for longer than the staleness threshold
        clock.advance(seconds=45)
        orchestrator.run_housekeeping()
        assert len(stale_events) == 1

        # ticks return -> another completed bar -> processed normally (2nd
        # call is a no-op per LeakyOnceStrategy, but proves the pipeline is
        # still alive, not stuck on the stale notification)
        feed.on_tick(_quote(instrument.instrument_key, "102", clock.now()))
        clock.advance(minutes=1, seconds=5)
        feed.on_tick(_quote(instrument.instrument_key, "103", clock.now()))

        assert strategy.call_count == 2
        with session_scope(factory) as session:
            position = PositionRepository(session).get_for(instrument.id, Mode.PAPER)
            assert position is not None
            assert position.qty == 100  # unchanged -- LeakyOnceStrategy stayed quiet, as scripted
            assert KillSwitch(session).is_tripped() is False


class TestTokenExpiryMidSession:
    """Every Upstox call fails with a real stale-token 401 (ADR-027's own
    "Invalid token used to access API" response) — the kill switch's circuit
    breaker must trip, and ADR-021's one-transaction-per-candle guarantee
    must leave zero partial state behind, not a half-written order."""

    def test_repeated_401s_trip_the_kill_switch_leaving_no_partial_state(
        self, factory: sessionmaker[Session], instrument: Instrument
    ) -> None:
        def handle_expired_token(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                401,
                json={
                    "status": "error",
                    "errors": [
                        {"errorCode": "UDAPI100050", "message": "Invalid token used to access API"}
                    ],
                },
            )

        client = httpx.Client(transport=httpx.MockTransport(handle_expired_token))
        strategy = _AlwaysLongStrategy()
        bus = EventBus()
        calendar = NSECalendar(holidays=set())
        feed = LiveFeed(_NoOpProvider(), bus, calendar, {instrument.instrument_key: [Interval.M1]})
        risk_cfg = _risk_cfg(max_consecutive_errors=3)

        orchestrator = Orchestrator(
            factory,
            feed,
            bus,
            {instrument.instrument_key: LiveStrategyRunner(instrument, strategy)},
            mode=Mode.LIVE,
            risk_cfg=risk_cfg,
            sizer=FixedFractionalSizer(risk_cfg.risk_per_trade_pct),
            cost_rates=ZERO_COSTS,
            paper_cfg=_paper_cfg(),
            initial_cash=risk_cfg.capital,
            strategy_name=strategy.name,
            strategy_params={},
            live_orders_enabled=True,  # two-key gate open -- exercises the real broker call
            upstox_client=client,
            upstox_access_token="stale-token",
            upstox_cfg=UpstoxConfig(),
        )
        orchestrator.start_strategy_run()

        for i in range(3):
            orchestrator.bus.publish(
                _candle(
                    instrument.instrument_key, datetime(2026, 1, 1, 9, 15 + i, tzinfo=UTC), "100"
                )
            )

        with session_scope(factory) as session:
            state = KillSwitch(session).state()
            assert state.tripped is True
            assert state.consecutive_errors == 3
            # get_funds() (the first authenticated call in _process_candle)
            # raises before any order is attempted, and the whole per-candle
            # transaction rolls back on that exception -- so even the 3
            # NEW-status Signal rows never survive to be inspected.
            assert OrderRepository(session).list_all() == []
            assert SignalRepository(session).list_all() == []

        client.close()
