from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from personaltrade.core.enums import Interval
from personaltrade.data.live.aggregator import CandleAggregator
from personaltrade.data.providers.base import Quote

KEY = "NSE_EQ|X"


def _quote(ltp: str, ltt: datetime, ltq: int = 10, key: str = KEY) -> Quote:
    return Quote(instrument_key=key, ltp=Decimal(ltp), ltq=ltq, ltt=ltt, close=Decimal("100"))


class TestUnsupportedInterval:
    def test_rejects_daily_interval(self) -> None:
        with pytest.raises(ValueError, match="1m/15m"):
            CandleAggregator(KEY, Interval.D1)


class TestSingleBucket:
    def test_first_tick_opens_bar_no_completion(self) -> None:
        agg = CandleAggregator(KEY, Interval.M1)
        result = agg.add_tick(_quote("100", datetime(2026, 1, 1, 9, 15, 0, tzinfo=UTC)))
        assert result is None

    def test_ticks_within_same_minute_update_high_low_close_volume(self) -> None:
        agg = CandleAggregator(KEY, Interval.M1)
        agg.add_tick(_quote("100", datetime(2026, 1, 1, 9, 15, 0, tzinfo=UTC), ltq=10))
        agg.add_tick(_quote("105", datetime(2026, 1, 1, 9, 15, 20, tzinfo=UTC), ltq=5))
        result = agg.add_tick(_quote("98", datetime(2026, 1, 1, 9, 15, 40, tzinfo=UTC), ltq=7))
        assert result is None  # still the same bucket

        completed = agg.flush()
        assert completed is not None
        assert completed.open == Decimal("100")
        assert completed.high == Decimal("105")
        assert completed.low == Decimal("98")
        assert completed.close == Decimal("98")
        assert completed.volume == 22  # 10+5+7
        assert completed.ts == datetime(2026, 1, 1, 9, 15, 0, tzinfo=UTC)


class TestBoundaryCrossing:
    def test_tick_in_next_bucket_closes_previous_bar(self) -> None:
        agg = CandleAggregator(KEY, Interval.M1)
        agg.add_tick(_quote("100", datetime(2026, 1, 1, 9, 15, 0, tzinfo=UTC), ltq=10))
        agg.add_tick(_quote("102", datetime(2026, 1, 1, 9, 15, 45, tzinfo=UTC), ltq=5))

        completed = agg.add_tick(_quote("103", datetime(2026, 1, 1, 9, 16, 5, tzinfo=UTC), ltq=3))
        assert completed is not None
        assert completed.ts == datetime(2026, 1, 1, 9, 15, 0, tzinfo=UTC)
        assert completed.open == Decimal("100")
        assert completed.close == Decimal("102")
        assert completed.volume == 15

    def test_new_bar_starts_fresh_after_boundary(self) -> None:
        agg = CandleAggregator(KEY, Interval.M1)
        agg.add_tick(_quote("100", datetime(2026, 1, 1, 9, 15, 0, tzinfo=UTC), ltq=10))
        agg.add_tick(_quote("103", datetime(2026, 1, 1, 9, 16, 5, tzinfo=UTC), ltq=3))

        completed = agg.flush()
        assert completed is not None
        assert completed.ts == datetime(2026, 1, 1, 9, 16, 0, tzinfo=UTC)
        assert completed.open == Decimal("103")
        assert completed.volume == 3

    def test_15m_interval_buckets_correctly(self) -> None:
        agg = CandleAggregator(KEY, Interval.M15)
        agg.add_tick(_quote("100", datetime(2026, 1, 1, 9, 15, 0, tzinfo=UTC)))
        # still within [09:15, 09:30)
        result = agg.add_tick(_quote("101", datetime(2026, 1, 1, 9, 29, 59, tzinfo=UTC)))
        assert result is None
        # crosses into [09:30, 09:45)
        completed = agg.add_tick(_quote("102", datetime(2026, 1, 1, 9, 30, 0, tzinfo=UTC)))
        assert completed is not None
        assert completed.ts == datetime(2026, 1, 1, 9, 15, 0, tzinfo=UTC)


class TestWrongInstrument:
    def test_tick_for_different_instrument_rejected(self) -> None:
        agg = CandleAggregator(KEY, Interval.M1)
        with pytest.raises(ValueError, match=r"NSE_EQ\|Y"):
            agg.add_tick(_quote("100", datetime(2026, 1, 1, 9, 15, 0, tzinfo=UTC), key="NSE_EQ|Y"))


class TestFlush:
    def test_flush_with_no_ticks_returns_none(self) -> None:
        agg = CandleAggregator(KEY, Interval.M1)
        assert agg.flush() is None

    def test_flush_is_idempotent_second_call_returns_none(self) -> None:
        agg = CandleAggregator(KEY, Interval.M1)
        agg.add_tick(_quote("100", datetime(2026, 1, 1, 9, 15, 0, tzinfo=UTC)))
        assert agg.flush() is not None
        assert agg.flush() is None
