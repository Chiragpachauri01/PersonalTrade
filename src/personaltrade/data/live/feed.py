"""Live feed orchestration (ROADMAP M10): the market-hours gate, tick -> candle
aggregation, staleness detection, and `CandleReceived`/`FeedStale` publication on
top of any `MarketDataProvider.stream_quotes()` — provider-agnostic (Rule 7).
Reconnection is entirely the provider's own concern (data/providers/upstox.py);
`LiveFeed` never sees a dropped connection, only a brief gap in ticks.

`check_staleness()` is not called on a timer here — there's no scheduler yet
(that's M11's APScheduler). It's a method ready for M11's orchestrator to poll
periodically, the same shape as the Paper Broker's `check_resting_orders()` (M9).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from personaltrade.core.calendar import NSECalendar
from personaltrade.core.clock import Clock, SystemClock
from personaltrade.core.enums import Interval
from personaltrade.core.events import CandleReceived, EventBus, FeedStale
from personaltrade.core.logging import get_logger
from personaltrade.data.live.aggregator import AggregatedCandle, CandleAggregator
from personaltrade.data.live.staleness import StalenessDetector
from personaltrade.data.providers.base import MarketDataProvider, Quote

logger = get_logger(__name__)


class LiveFeed:
    def __init__(
        self,
        provider: MarketDataProvider,
        bus: EventBus,
        calendar: NSECalendar,
        subscriptions: dict[str, list[Interval]],
        *,
        staleness_threshold: timedelta = timedelta(seconds=30),
        clock: Clock | None = None,
    ) -> None:
        """`subscriptions` maps instrument_key -> the bar intervals to build for
        it (e.g. {"NSE_EQ|...": [Interval.M1, Interval.M15]})."""
        if not subscriptions:
            raise ValueError("subscriptions must be non-empty")
        self.provider = provider
        self.bus = bus
        self.calendar = calendar
        self.clock = clock or SystemClock()
        self._aggregators: dict[str, list[CandleAggregator]] = {
            key: [CandleAggregator(key, interval) for interval in intervals]
            for key, intervals in subscriptions.items()
        }
        self._staleness = StalenessDetector(staleness_threshold, self.clock)
        self._stale_notified = False

    @property
    def instrument_keys(self) -> list[str]:
        return list(self._aggregators)

    def is_market_open(self, now: datetime | None = None) -> bool:
        return self.calendar.is_open_at(now if now is not None else self.clock.now())

    async def run(self) -> None:
        """Consumes `provider.stream_quotes()` until it ends (it doesn't, under
        normal operation — the provider reconnects transparently) or the
        caller cancels this coroutine. No-op if the market isn't open."""
        if not self.is_market_open():
            logger.info("live_feed_market_closed_not_starting")
            return
        async for quote in self.provider.stream_quotes(self.instrument_keys):
            self.on_tick(quote)

    def on_tick(self, quote: Quote) -> None:
        self._staleness.record_tick(quote.ltt)
        self._stale_notified = False
        for aggregator in self._aggregators.get(quote.instrument_key, []):
            completed = aggregator.add_tick(quote)
            if completed is not None:
                self._publish_candle(completed)

    def check_staleness(self, now: datetime | None = None) -> bool:
        """Publishes `FeedStale` the moment staleness is first detected (not on
        every subsequent poll while still stale — edge-triggered, like the Kill
        Switch's trip()). Returns whether the feed is currently stale."""
        current = now if now is not None else self.clock.now()
        stale = self._staleness.is_stale(current)
        if stale and not self._stale_notified:
            self.bus.publish(
                FeedStale(
                    instrument_key=None,
                    last_tick_at=self._staleness.last_tick_at,
                    detected_at=current,
                )
            )
            self._stale_notified = True
        return stale

    def flush(self) -> None:
        """End of session: emit every in-progress bar, so the final partial bar
        of the day isn't silently dropped just because no later tick closed it."""
        for aggregators in self._aggregators.values():
            for aggregator in aggregators:
                completed = aggregator.flush()
                if completed is not None:
                    self._publish_candle(completed)

    def _publish_candle(self, candle: AggregatedCandle) -> None:
        self.bus.publish(
            CandleReceived(
                instrument_key=candle.instrument_key,
                interval=candle.interval,
                ts=candle.ts,
                open=candle.open,
                high=candle.high,
                low=candle.low,
                close=candle.close,
                volume=candle.volume,
            )
        )
