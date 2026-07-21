"""The Orchestrator (ROADMAP M11): subscribes to the live feed's `CandleReceived`
events and drives candle -> strategy -> risk -> broker -> persistence for every
subscribed instrument. Owns the invariant that risk is the only path to a
broker (CLAUDE.md Rule 14) — `place_order()` is only ever called with an
`ApprovedOrder` risk.engine.RiskEngine.evaluate() itself produced.

Every candle is processed inside one committed transaction (signal, risk
decision, and any resulting order all commit — or none of them do, on error).
A handler exception is contained here and fed to the kill switch's circuit
breaker rather than propagating into the live feed's event dispatch
(core/events.py's EventBus has no handler isolation of its own) — one bad
signal must never take down the whole session.

Every signal a strategy produces is persisted as a `Signal` row (ROADMAP M12
needs this for per-strategy P&L attribution and the trade journal's
entry/exit context snapshots) — `context` carries whatever indicator values
the strategy attached, and `status` records whether risk approved or rejected
it. Approved signals link forward to the `Order` they produced via
`Order.signal_id`, set once the broker acknowledges it.
"""

from __future__ import annotations

import asyncio
import contextlib
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from personaltrade.core.calendar import ist_midnight_utc
from personaltrade.core.clock import Clock, SystemClock
from personaltrade.core.config import CostConfig, PaperConfig, RiskConfig
from personaltrade.core.enums import Mode, SignalStatus
from personaltrade.core.events import CandleReceived, EventBus, FeedStale
from personaltrade.core.logging import get_logger
from personaltrade.data.live.feed import LiveFeed
from personaltrade.data.store.db import session_scope
from personaltrade.data.store.models import Signal as SignalRow
from personaltrade.data.store.models import StrategyRun
from personaltrade.data.store.repos import (
    OrderRepository,
    PositionRepository,
    SignalRepository,
    StrategyRunRepository,
    TradeRepository,
)
from personaltrade.execution.broker import OrderRequest
from personaltrade.execution.paper.broker import PaperBroker
from personaltrade.execution.paper.quotes import LiveQuoteSource
from personaltrade.orchestrator.reconcile import ReconciliationFinding, reconcile_on_startup
from personaltrade.orchestrator.runner import LiveStrategyRunner
from personaltrade.risk.engine import Rejection, RiskEngine
from personaltrade.risk.kill_switch import KillSwitch
from personaltrade.risk.sizing import PositionSizer
from personaltrade.strategy.base import FLAT_POSITION, PositionView

logger = get_logger(__name__)

_HOUSEKEEPING_LOG = "orchestrator_housekeeping_failed"


class Orchestrator:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        feed: LiveFeed,
        bus: EventBus,
        runners: dict[str, LiveStrategyRunner],
        *,
        mode: Mode,
        risk_cfg: RiskConfig,
        sizer: PositionSizer,
        cost_rates: CostConfig,
        paper_cfg: PaperConfig,
        initial_cash: Decimal,
        strategy_name: str,
        strategy_params: dict[str, Any],
        clock: Clock | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.feed = feed
        self.bus = bus
        self.runners = runners
        self.mode = mode
        self.risk_cfg = risk_cfg
        self.sizer = sizer
        self.cost_rates = cost_rates
        self.paper_cfg = paper_cfg
        self.initial_cash = initial_cash
        self.strategy_name = strategy_name
        self.strategy_params = strategy_params
        self.strategy_run_id: int | None = None
        self.clock = clock or SystemClock()
        self.quote_source = LiveQuoteSource()
        self._feed_task: asyncio.Task[None] | None = None

        bus.subscribe(CandleReceived, self._on_candle)
        bus.subscribe(FeedStale, self._on_feed_stale)

    def start_strategy_run(self) -> int:
        """Call once at startup, before any candle is processed — every
        `Signal` row this run persists links here, which is how ROADMAP M12
        attributes P&L to a strategy."""
        with session_scope(self.session_factory) as session:
            run = StrategyRunRepository(session).add(
                StrategyRun(
                    strategy_name=self.strategy_name, params=self.strategy_params, mode=self.mode
                )
            )
            self.strategy_run_id = run.id
        return self.strategy_run_id

    def reconcile(self) -> list[ReconciliationFinding]:
        with session_scope(self.session_factory) as session:
            findings = reconcile_on_startup(session, self.mode)
        for finding in findings:
            logger.warning(
                "startup_reconciliation_finding",
                client_order_id=finding.client_order_id,
                was_state=str(finding.was_state),
            )
        return findings

    async def start_feed(self) -> None:
        if self._feed_task is not None and not self._feed_task.done():
            return
        self._feed_task = asyncio.create_task(self.feed.run())

    async def stop_feed(self) -> None:
        if self._feed_task is None:
            return
        self._feed_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._feed_task
        self._feed_task = None
        self.feed.flush()

    def run_housekeeping(self) -> None:
        """Periodic tick (the scheduler calls this every few seconds while a
        session is live): resting-order fills and staleness detection — both
        M9/M10 built the mechanism and left "who calls it, how often" for here."""
        try:
            with session_scope(self.session_factory) as session:
                self._build_broker(session).check_resting_orders()
            self.feed.check_staleness()
        except Exception:
            logger.exception(_HOUSEKEEPING_LOG)

    def _build_broker(self, session: Session) -> PaperBroker:
        return PaperBroker(
            session,
            self.quote_source,
            cost_rates=self.cost_rates,
            paper_cfg=self.paper_cfg,
            initial_cash=self.initial_cash,
            clock=self.clock,
        )

    def _on_candle(self, event: CandleReceived) -> None:
        self.quote_source.update(event.instrument_key, event.close)
        runner = self.runners.get(event.instrument_key)
        if runner is None:
            return
        try:
            with session_scope(self.session_factory) as session:
                self._process_candle(session, runner, event)
        except Exception:
            logger.exception(
                "orchestrator_candle_handling_failed", instrument_key=event.instrument_key
            )
            try:
                with session_scope(self.session_factory) as session:
                    KillSwitch(session).record_error(
                        self.risk_cfg.kill_switch.max_consecutive_errors
                    )
            except Exception:
                logger.exception("orchestrator_kill_switch_record_error_failed")

    def _on_feed_stale(self, event: FeedStale) -> None:
        logger.warning("feed_stale", last_tick_at=event.last_tick_at, detected_at=event.detected_at)

    def _process_candle(
        self, session: Session, runner: LiveStrategyRunner, event: CandleReceived
    ) -> None:
        position_row = PositionRepository(session).get_for(runner.instrument.id, self.mode)
        position_view = (
            FLAT_POSITION
            if position_row is None or position_row.qty == 0
            else PositionView(qty=position_row.qty, avg_price=float(position_row.avg_price))
        )
        signal = runner.on_candle(event, position_view)
        if signal is None:
            return
        if self.strategy_run_id is None:
            raise RuntimeError("start_strategy_run() must be called before processing candles")

        signal_row = SignalRepository(session).add(
            SignalRow(
                instrument_id=runner.instrument.id,
                strategy_run_id=self.strategy_run_id,
                direction=signal.direction,
                ref_price=Decimal(str(signal.ref_price)),
                context=signal.context,
                status=SignalStatus.NEW,
            )
        )

        broker = self._build_broker(session)
        risk_engine = RiskEngine(session, self.risk_cfg, self.sizer)
        equity = broker.get_funds().equity
        since = ist_midnight_utc(self.clock.now())
        daily_realized_pnl = TradeRepository(session).sum_realized_pnl_since(self.mode, since)

        result = risk_engine.evaluate(
            signal,
            instrument=runner.instrument,
            mode=self.mode,
            equity=equity,
            daily_realized_pnl=daily_realized_pnl,
        )
        if isinstance(result, Rejection):
            signal_row.status = SignalStatus.REJECTED
            logger.info(
                "signal_rejected",
                symbol=runner.instrument.symbol,
                reason=result.reason.value,
                detail=result.detail,
            )
            return

        signal_row.status = SignalStatus.APPROVED
        order_request = OrderRequest(
            client_order_id=result.client_order_id,
            instrument_id=result.instrument_id,
            side=result.side,
            order_type=result.order_type,
            qty=result.qty,
            limit_price=result.limit_price,
        )
        ack = broker.place_order(order_request)
        db_order = OrderRepository(session).get_by_client_order_id(ack.client_order_id)
        if db_order is not None:
            db_order.signal_id = signal_row.id
        KillSwitch(session).record_success()
        logger.info(
            "order_placed",
            symbol=runner.instrument.symbol,
            client_order_id=ack.client_order_id,
            broker_order_id=ack.broker_order_id,
            side=str(result.side),
            qty=result.qty,
        )
