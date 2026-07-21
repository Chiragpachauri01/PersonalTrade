"""Reconnect/backoff policy (ROADMAP M10) — pure delay math, no I/O. Used by
`UpstoxMarketData.stream_quotes()` (upstox.py) to decide how long to wait
before retrying a dropped websocket connection; consumers of `stream_quotes()`
never see the drop, only a brief gap in ticks.
"""

from __future__ import annotations


class ReconnectPolicy:
    """Exponential backoff with a cap: `base_delay * factor**attempt`, capped at
    `max_delay`. `attempt` is caller-tracked (0 for the first retry) — this class
    holds no mutable state so it's trivially safe to share across connections."""

    def __init__(
        self, base_delay: float = 1.0, max_delay: float = 30.0, factor: float = 2.0
    ) -> None:
        if base_delay <= 0:
            raise ValueError(f"base_delay must be > 0, got {base_delay}")
        if max_delay < base_delay:
            raise ValueError(f"max_delay ({max_delay}) must be >= base_delay ({base_delay})")
        if factor <= 1:
            raise ValueError(f"factor must be > 1, got {factor}")
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.factor = factor

    def delay_for(self, attempt: int) -> float:
        if attempt < 0:
            raise ValueError(f"attempt must be >= 0, got {attempt}")
        return min(self.base_delay * (self.factor**attempt), self.max_delay)
