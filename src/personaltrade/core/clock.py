"""Injectable time source — lets order/fill timestamps (and simulated latency, M9's
Paper Broker) be deterministic in tests without monkeypatching datetime.now."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)
