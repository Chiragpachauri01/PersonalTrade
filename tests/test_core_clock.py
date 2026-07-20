from __future__ import annotations

from datetime import timedelta

from personaltrade.core.clock import SystemClock


def test_system_clock_returns_tz_aware_utc() -> None:
    now = SystemClock().now()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)
