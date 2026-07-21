"""APScheduler wiring (ROADMAP M11): session start/stop and periodic
housekeeping. `AsyncIOScheduler` because the whole process already lives
inside one asyncio event loop (the live feed's async generator chain, M10).

This class only decides *when* — all the actual trading logic it triggers
lives on `Orchestrator` (start_feed/stop_feed/run_housekeeping/reconcile).
"""

from __future__ import annotations

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from personaltrade.core.calendar import IST, MARKET_CLOSE, MARKET_OPEN, NSECalendar
from personaltrade.core.clock import Clock, SystemClock
from personaltrade.core.logging import get_logger
from personaltrade.orchestrator.service import Orchestrator

logger = get_logger(__name__)

HOUSEKEEPING_INTERVAL_SECONDS = 10


class LiveScheduler:
    def __init__(
        self, orchestrator: Orchestrator, calendar: NSECalendar, clock: Clock | None = None
    ) -> None:
        self.orchestrator = orchestrator
        self.calendar = calendar
        self.clock = clock or SystemClock()
        self.scheduler = AsyncIOScheduler(timezone=IST)
        self.scheduler.add_job(
            self._session_start,
            CronTrigger(hour=MARKET_OPEN.hour, minute=MARKET_OPEN.minute, timezone=IST),
            id="session_start",
        )
        self.scheduler.add_job(
            self._session_stop,
            CronTrigger(hour=MARKET_CLOSE.hour, minute=MARKET_CLOSE.minute, timezone=IST),
            id="session_stop",
        )
        self.scheduler.add_job(
            self._housekeeping,
            "interval",
            seconds=HOUSEKEEPING_INTERVAL_SECONDS,
            id="housekeeping",
        )

    async def start(self) -> None:
        self.scheduler.start()
        # `pt run` can start mid-session (between two daily cron firings) —
        # without this, the feed would only ever start at the NEXT day's
        # 09:15 IST trigger, leaving today's remaining session untraded.
        if self.calendar.is_open_at(self.clock.now()):
            logger.info("session_already_open_starting_feed_immediately")
            await self.orchestrator.start_feed()

    def shutdown(self) -> None:
        """Safe to call even if `start()` was never reached (e.g. `pt run`'s
        `finally` block, after an exception earlier in setup)."""
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    async def _session_start(self) -> None:
        today = self.clock.now().astimezone(IST).date()
        if not self.calendar.is_trading_day(today):
            logger.info("session_start_skipped_non_trading_day", date=str(today))
            return
        logger.info("session_starting")
        await self.orchestrator.start_feed()

    async def _session_stop(self) -> None:
        logger.info("session_stopping")
        await self.orchestrator.stop_feed()

    async def _housekeeping(self) -> None:
        self.orchestrator.run_housekeeping()
