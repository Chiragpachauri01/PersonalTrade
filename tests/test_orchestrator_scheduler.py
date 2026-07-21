"""LiveScheduler (ROADMAP M11): job registration, immediate-start-if-already-
open logic, and the non-trading-day skip. Uses a mocked Orchestrator since
this tests only the scheduler's own timing/wiring logic — Orchestrator's own
behavior is already covered by test_orchestrator_service.py.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import cast
from unittest.mock import AsyncMock, MagicMock

from personaltrade.core.calendar import NSECalendar
from personaltrade.core.clock import Clock
from personaltrade.orchestrator.scheduler import LiveScheduler
from personaltrade.orchestrator.service import Orchestrator
from tests.factories import ManualClock

OPEN_TIME = datetime(2026, 7, 17, 5, 0, tzinfo=UTC)  # Friday, 10:30 IST
CLOSED_TIME = datetime(2026, 7, 18, 5, 0, tzinfo=UTC)  # Saturday


def _mock_orchestrator() -> MagicMock:
    mock = MagicMock(spec=Orchestrator)
    mock.start_feed = AsyncMock()
    mock.stop_feed = AsyncMock()
    mock.run_housekeeping = MagicMock()
    return mock


def _scheduler(
    orchestrator: MagicMock, calendar: NSECalendar, clock: Clock | None = None
) -> LiveScheduler:
    # `MagicMock(spec=Orchestrator)` satisfies isinstance(mock, Orchestrator) at
    # runtime (that's what spec= guarantees) but mypy can't see that structurally —
    # narrow, single-purpose cast right at the constructor boundary.
    return LiveScheduler(cast(Orchestrator, orchestrator), calendar, clock=clock)


class TestJobRegistration:
    def test_registers_all_three_jobs(self) -> None:
        scheduler = _scheduler(_mock_orchestrator(), NSECalendar(holidays=set()))
        job_ids = {job.id for job in scheduler.scheduler.get_jobs()}
        assert job_ids == {"session_start", "session_stop", "housekeeping"}
        scheduler.shutdown()


class TestStartImmediateSession:
    def test_starts_feed_immediately_if_already_within_market_hours(self) -> None:
        orchestrator = _mock_orchestrator()
        scheduler = _scheduler(
            orchestrator, NSECalendar(holidays=set()), clock=ManualClock(OPEN_TIME)
        )

        async def _run() -> None:
            await scheduler.start()
            # AsyncIOScheduler binds to the loop it started on — shutdown()
            # must happen before that loop closes (i.e. inside this same
            # asyncio.run), not as a later, separate sync call.
            scheduler.shutdown()

        asyncio.run(_run())
        orchestrator.start_feed.assert_awaited_once()

    def test_does_not_start_feed_if_market_closed(self) -> None:
        orchestrator = _mock_orchestrator()
        scheduler = _scheduler(
            orchestrator, NSECalendar(holidays=set()), clock=ManualClock(CLOSED_TIME)
        )

        async def _run() -> None:
            await scheduler.start()
            scheduler.shutdown()

        asyncio.run(_run())
        orchestrator.start_feed.assert_not_awaited()


class TestSessionStartJob:
    def test_skips_on_non_trading_day(self) -> None:
        orchestrator = _mock_orchestrator()
        scheduler = _scheduler(
            orchestrator, NSECalendar(holidays=set()), clock=ManualClock(CLOSED_TIME)
        )
        asyncio.run(scheduler._session_start())
        orchestrator.start_feed.assert_not_awaited()
        scheduler.shutdown()

    def test_starts_feed_on_a_trading_day(self) -> None:
        orchestrator = _mock_orchestrator()
        scheduler = _scheduler(
            orchestrator, NSECalendar(holidays=set()), clock=ManualClock(OPEN_TIME)
        )
        asyncio.run(scheduler._session_start())
        orchestrator.start_feed.assert_awaited_once()
        scheduler.shutdown()


class TestSessionStopAndHousekeeping:
    def test_session_stop_calls_orchestrator_stop_feed(self) -> None:
        orchestrator = _mock_orchestrator()
        scheduler = _scheduler(orchestrator, NSECalendar(holidays=set()))
        asyncio.run(scheduler._session_stop())
        orchestrator.stop_feed.assert_awaited_once()
        scheduler.shutdown()

    def test_housekeeping_calls_orchestrator_run_housekeeping(self) -> None:
        orchestrator = _mock_orchestrator()
        scheduler = _scheduler(orchestrator, NSECalendar(holidays=set()))
        asyncio.run(scheduler._housekeeping())
        orchestrator.run_housekeeping.assert_called_once()
        scheduler.shutdown()
