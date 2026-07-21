"""Staleness detection (ROADMAP M10 risk: "websocket instability" — a connection
can stay technically open while silently delivering nothing). `LiveFeed` polls
this on each new candle bucket boundary and publishes `FeedStale` if tripped.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from personaltrade.core.clock import Clock, SystemClock


class StalenessDetector:
    def __init__(self, threshold: timedelta, clock: Clock | None = None) -> None:
        self.threshold = threshold
        self.clock = clock or SystemClock()
        self._last_tick_at: datetime | None = None

    @property
    def last_tick_at(self) -> datetime | None:
        return self._last_tick_at

    def record_tick(self, at: datetime | None = None) -> None:
        self._last_tick_at = at if at is not None else self.clock.now()

    def is_stale(self, now: datetime | None = None) -> bool:
        """True once `threshold` has passed since the last tick — or immediately
        if no tick has arrived yet (there's nothing to be "current" about)."""
        if self._last_tick_at is None:
            return True
        current = now if now is not None else self.clock.now()
        return current - self._last_tick_at >= self.threshold
