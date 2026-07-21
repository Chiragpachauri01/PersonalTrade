from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from personaltrade.core.enums import Interval
from personaltrade.core.events import CandleReceived, EventBus, FeedStale


def _candle(instrument_key: str = "NSE_EQ|X") -> CandleReceived:
    return CandleReceived(
        instrument_key=instrument_key,
        interval=Interval.M1,
        ts=datetime(2026, 1, 1, tzinfo=UTC),
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100.5"),
        volume=10,
    )


class TestEventBus:
    def test_subscriber_receives_published_event(self) -> None:
        bus = EventBus()
        received: list[CandleReceived] = []
        bus.subscribe(CandleReceived, received.append)

        event = _candle()
        bus.publish(event)

        assert received == [event]

    def test_multiple_subscribers_called_in_order(self) -> None:
        bus = EventBus()
        calls: list[str] = []
        bus.subscribe(CandleReceived, lambda e: calls.append("first"))
        bus.subscribe(CandleReceived, lambda e: calls.append("second"))

        bus.publish(_candle())

        assert calls == ["first", "second"]

    def test_handlers_only_fire_for_their_own_event_type(self) -> None:
        bus = EventBus()
        candle_calls: list[CandleReceived] = []
        stale_calls: list[FeedStale] = []
        bus.subscribe(CandleReceived, candle_calls.append)
        bus.subscribe(FeedStale, stale_calls.append)

        bus.publish(_candle())

        assert len(candle_calls) == 1
        assert stale_calls == []

    def test_publish_with_no_subscribers_is_a_noop(self) -> None:
        bus = EventBus()
        bus.publish(_candle())  # must not raise
