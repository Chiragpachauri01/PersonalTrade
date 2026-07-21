from __future__ import annotations

from datetime import timedelta

from personaltrade.data.live.staleness import StalenessDetector
from tests.factories import ManualClock


class TestStalenessDetector:
    def test_stale_before_any_tick(self) -> None:
        detector = StalenessDetector(timedelta(seconds=30), ManualClock())
        assert detector.is_stale() is True
        assert detector.last_tick_at is None

    def test_not_stale_immediately_after_a_tick(self) -> None:
        clock = ManualClock()
        detector = StalenessDetector(timedelta(seconds=30), clock)
        detector.record_tick()
        assert detector.is_stale() is False

    def test_not_stale_before_threshold_elapses(self) -> None:
        clock = ManualClock()
        detector = StalenessDetector(timedelta(seconds=30), clock)
        detector.record_tick()
        clock.advance(seconds=29)
        assert detector.is_stale() is False

    def test_stale_once_threshold_elapses(self) -> None:
        clock = ManualClock()
        detector = StalenessDetector(timedelta(seconds=30), clock)
        detector.record_tick()
        clock.advance(seconds=30)
        assert detector.is_stale() is True

    def test_new_tick_clears_staleness(self) -> None:
        clock = ManualClock()
        detector = StalenessDetector(timedelta(seconds=30), clock)
        detector.record_tick()
        clock.advance(seconds=45)
        assert detector.is_stale() is True

        detector.record_tick()
        assert detector.is_stale() is False

    def test_explicit_tick_timestamp_used_over_clock(self) -> None:
        clock = ManualClock()
        detector = StalenessDetector(timedelta(seconds=30), clock)
        detector.record_tick(at=clock.now())
        clock.advance(seconds=10)
        assert detector.last_tick_at == clock.now() - timedelta(seconds=10)
