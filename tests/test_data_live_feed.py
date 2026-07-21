"""LiveFeed orchestration (ROADMAP M10): the market-hours gate, tick
aggregation -> CandleReceived publication, staleness -> FeedStale, and flush.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pandas as pd
import pytest

from personaltrade.core.calendar import NSECalendar
from personaltrade.core.enums import Interval
from personaltrade.core.events import CandleReceived, EventBus, FeedStale
from personaltrade.data.live.feed import LiveFeed
from personaltrade.data.providers.base import InstrumentInfo, Quote
from tests.factories import ManualClock

KEY = "NSE_EQ|X"
OPEN_TIME = datetime(2026, 7, 17, 5, 0, tzinfo=UTC)  # Friday, 10:30 IST — regular session
CLOSED_TIME = datetime(2026, 7, 18, 5, 0, tzinfo=UTC)  # Saturday


class ScriptedProvider:
    """MarketDataProvider test double: yields a fixed tick sequence then stops."""

    def __init__(self, ticks: list[Quote]) -> None:
        self.ticks = ticks
        self.requested_keys: list[str] | None = None

    def get_instruments(self, exchange: str = "NSE") -> list[InstrumentInfo]:
        raise NotImplementedError

    def get_historical_candles(
        self, instrument_key: str, interval: Interval, from_date: date, to_date: date
    ) -> pd.DataFrame:
        raise NotImplementedError

    async def stream_quotes(self, instrument_keys: list[str]) -> AsyncGenerator[Quote, None]:
        self.requested_keys = instrument_keys
        for tick in self.ticks:
            yield tick


def _quote(ltp: str, ltt: datetime, ltq: int = 10, key: str = KEY) -> Quote:
    return Quote(instrument_key=key, ltp=Decimal(ltp), ltq=ltq, ltt=ltt, close=Decimal("100"))


@pytest.fixture()
def calendar() -> NSECalendar:
    return NSECalendar(holidays=set())


class TestConstruction:
    def test_empty_subscriptions_rejected(self, calendar: NSECalendar) -> None:
        with pytest.raises(ValueError, match="subscriptions"):
            LiveFeed(ScriptedProvider([]), EventBus(), calendar, {})


class TestMarketHoursGate:
    def test_run_is_a_noop_when_market_closed(self, calendar: NSECalendar) -> None:
        provider = ScriptedProvider([_quote("100", CLOSED_TIME)])
        feed = LiveFeed(
            provider, EventBus(), calendar, {KEY: [Interval.M1]}, clock=ManualClock(CLOSED_TIME)
        )

        asyncio.run(feed.run())

        assert provider.requested_keys is None  # never even subscribed

    def test_run_consumes_ticks_when_market_open(self, calendar: NSECalendar) -> None:
        provider = ScriptedProvider([_quote("100", OPEN_TIME)])
        feed = LiveFeed(
            provider, EventBus(), calendar, {KEY: [Interval.M1]}, clock=ManualClock(OPEN_TIME)
        )

        asyncio.run(feed.run())

        assert provider.requested_keys == [KEY]


class TestCandleAggregationAndPublishing:
    def test_completed_bar_publishes_candle_received(self, calendar: NSECalendar) -> None:
        t1 = OPEN_TIME + timedelta(minutes=1, seconds=5)
        provider = ScriptedProvider([_quote("100", OPEN_TIME), _quote("103", t1)])
        bus = EventBus()
        received: list[CandleReceived] = []
        bus.subscribe(CandleReceived, received.append)
        feed = LiveFeed(provider, bus, calendar, {KEY: [Interval.M1]}, clock=ManualClock(OPEN_TIME))

        asyncio.run(feed.run())

        assert len(received) == 1
        assert received[0].instrument_key == KEY
        assert received[0].close == Decimal("100")

    def test_multiple_intervals_for_same_instrument_close_independently(
        self, calendar: NSECalendar
    ) -> None:
        t1 = OPEN_TIME + timedelta(minutes=1, seconds=5)  # crosses a 1m boundary, not 15m
        provider = ScriptedProvider([_quote("100", OPEN_TIME), _quote("103", t1)])
        bus = EventBus()
        received: list[CandleReceived] = []
        bus.subscribe(CandleReceived, received.append)
        feed = LiveFeed(
            provider,
            bus,
            calendar,
            {KEY: [Interval.M1, Interval.M15]},
            clock=ManualClock(OPEN_TIME),
        )

        asyncio.run(feed.run())

        assert [r.interval for r in received] == [Interval.M1]

    def test_ticks_for_unsubscribed_instrument_are_ignored(self, calendar: NSECalendar) -> None:
        provider = ScriptedProvider([_quote("100", OPEN_TIME, key="NSE_EQ|OTHER")])
        bus = EventBus()
        received: list[CandleReceived] = []
        bus.subscribe(CandleReceived, received.append)
        feed = LiveFeed(provider, bus, calendar, {KEY: [Interval.M1]}, clock=ManualClock(OPEN_TIME))

        asyncio.run(feed.run())

        assert received == []


class TestStaleness:
    def test_stale_before_any_tick(self, calendar: NSECalendar) -> None:
        feed = LiveFeed(
            ScriptedProvider([]),
            EventBus(),
            calendar,
            {KEY: [Interval.M1]},
            staleness_threshold=timedelta(seconds=30),
            clock=ManualClock(OPEN_TIME),
        )
        assert feed.check_staleness() is True

    def test_publishes_feedstale_once_edge_triggered(self, calendar: NSECalendar) -> None:
        clock = ManualClock(OPEN_TIME)
        bus = EventBus()
        stale_events: list[FeedStale] = []
        bus.subscribe(FeedStale, stale_events.append)
        feed = LiveFeed(
            ScriptedProvider([_quote("100", OPEN_TIME)]),
            bus,
            calendar,
            {KEY: [Interval.M1]},
            staleness_threshold=timedelta(seconds=30),
            clock=clock,
        )

        asyncio.run(feed.run())  # records a tick at OPEN_TIME
        clock.advance(seconds=31)
        assert feed.check_staleness() is True
        assert feed.check_staleness() is True  # still stale — must not re-publish
        assert len(stale_events) == 1

    def test_new_tick_clears_notification_so_next_staleness_republishes(
        self, calendar: NSECalendar
    ) -> None:
        clock = ManualClock(OPEN_TIME)
        bus = EventBus()
        stale_events: list[FeedStale] = []
        bus.subscribe(FeedStale, stale_events.append)
        feed = LiveFeed(
            ScriptedProvider([]),
            bus,
            calendar,
            {KEY: [Interval.M1]},
            staleness_threshold=timedelta(seconds=30),
            clock=clock,
        )

        feed.check_staleness()  # never ticked -> stale -> 1st event
        feed.on_tick(_quote("100", clock.now()))
        assert feed.check_staleness() is False

        clock.advance(seconds=31)
        assert feed.check_staleness() is True  # 2nd event
        assert len(stale_events) == 2


class TestFlush:
    def test_flush_emits_the_in_progress_bar(self, calendar: NSECalendar) -> None:
        bus = EventBus()
        received: list[CandleReceived] = []
        bus.subscribe(CandleReceived, received.append)
        feed = LiveFeed(
            ScriptedProvider([]),
            bus,
            calendar,
            {KEY: [Interval.M1]},
            clock=ManualClock(OPEN_TIME),
        )

        feed.on_tick(_quote("100", OPEN_TIME))
        assert received == []  # bar not closed by a boundary-crossing tick yet

        feed.flush()
        assert len(received) == 1
        assert received[0].close == Decimal("100")
