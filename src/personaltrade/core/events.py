"""In-process event bus (ADR-004): typed pub/sub, synchronous dispatch by default.

Events are pydantic models — typed, loggable, replayable in tests
(docs/architecture/01-system-architecture.md). Handlers run synchronously, in
subscription order, on the publisher's own call stack — no threads, no queue.
Keep handlers small; slow work is a scheduled job, not a handler.

Only the events ROADMAP M10 (Live Market Data Feed) needs are defined here;
the rest of the architecture doc's event vocabulary (SignalGenerated,
OrderSubmitted, ...) arrives with the milestones that produce them.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Any, TypeVar, cast

from pydantic import BaseModel

from personaltrade.core.enums import Interval

EventT = TypeVar("EventT", bound=BaseModel)


class CandleReceived(BaseModel):
    """A completed OHLCV bar from the live feed (data/live/feed.py)."""

    model_config = {"frozen": True}

    instrument_key: str
    interval: Interval
    ts: datetime  # bar start, UTC
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int


class FeedStale(BaseModel):
    """No tick received for at least the configured threshold — the websocket
    connection may be silently dead without having dropped."""

    model_config = {"frozen": True}

    instrument_key: str | None  # None = no ticks on ANY subscription
    last_tick_at: datetime | None
    detected_at: datetime


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[type[BaseModel], list[Callable[[Any], None]]] = defaultdict(list)

    def subscribe(self, event_type: type[EventT], handler: Callable[[EventT], None]) -> None:
        """`handler` is stored behind an erased `Callable[[Any], None]` — sound
        because `publish` only ever looks it up keyed by `type(event) is
        event_type`, so it's only ever called with an instance of the type it
        was registered for."""
        self._handlers[event_type].append(cast("Callable[[Any], None]", handler))

    def publish(self, event: BaseModel) -> None:
        for handler in self._handlers.get(type(event), []):
            handler(event)
