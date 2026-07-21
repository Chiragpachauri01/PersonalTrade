"""Tick -> OHLCV bar aggregation (ROADMAP M10). Pure: no I/O, no provider
knowledge — feeds off `Quote` (data/providers/base.py), the same DTO regardless
of which `MarketDataProvider` produced it (Rule 7).

Bucket boundaries are raw UTC-epoch-aligned: a 1-minute bucket boundary is the
same instant in every timezone, unlike the historical pipeline's IST *trading
day* boundaries (core/calendar.py::ist_trading_date), which are inherently
timezone-dependent. No such dependency exists at 1m/15m granularity.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from personaltrade.core.enums import Interval
from personaltrade.data.providers.base import Quote

#: Only sub-day intervals are buildable from ticks; 1d candles come from the
#: historical pipeline (M4), not tick aggregation.
_INTERVAL_SECONDS: dict[Interval, int] = {
    Interval.M1: 60,
    Interval.M15: 15 * 60,
}


@dataclass(frozen=True)
class AggregatedCandle:
    instrument_key: str
    interval: Interval
    ts: datetime  # bucket start, UTC
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int


class CandleAggregator:
    """One instrument, one interval. `LiveFeed` (feed.py) owns one instance per
    (instrument_key, interval) subscription."""

    def __init__(self, instrument_key: str, interval: Interval) -> None:
        if interval not in _INTERVAL_SECONDS:
            raise ValueError(f"can only aggregate ticks into 1m/15m bars, got {interval}")
        self.instrument_key = instrument_key
        self.interval = interval
        self._bucket_start: datetime | None = None
        self._open = Decimal(0)
        self._high = Decimal(0)
        self._low = Decimal(0)
        self._close = Decimal(0)
        self._volume = 0

    def add_tick(self, quote: Quote) -> AggregatedCandle | None:
        """Feed one tick for this aggregator's instrument. Returns the
        just-completed bar if this tick's bucket differs from the in-progress
        one (i.e. it crossed a boundary), else None."""
        if quote.instrument_key != self.instrument_key:
            raise ValueError(
                f"tick for {quote.instrument_key!r} fed to aggregator for {self.instrument_key!r}"
            )
        bucket = self._bucket_start_for(quote.ltt)
        completed = None
        if self._bucket_start is not None and bucket != self._bucket_start:
            completed = self._snapshot()
            self._bucket_start = None

        if self._bucket_start is None:
            self._bucket_start = bucket
            self._open = self._high = self._low = quote.ltp
            self._volume = 0

        self._high = max(self._high, quote.ltp)
        self._low = min(self._low, quote.ltp)
        self._close = quote.ltp
        self._volume += quote.ltq
        return completed

    def flush(self) -> AggregatedCandle | None:
        """End-of-session: emit the in-progress bar even though no later tick
        naturally closed it. None if no tick has arrived yet."""
        if self._bucket_start is None:
            return None
        candle = self._snapshot()
        self._bucket_start = None
        return candle

    def _bucket_start_for(self, ts: datetime) -> datetime:
        seconds = _INTERVAL_SECONDS[self.interval]
        epoch = int(ts.timestamp())
        return datetime.fromtimestamp(epoch - (epoch % seconds), tz=UTC)

    def _snapshot(self) -> AggregatedCandle:
        assert self._bucket_start is not None
        return AggregatedCandle(
            instrument_key=self.instrument_key,
            interval=self.interval,
            ts=self._bucket_start,
            open=self._open,
            high=self._high,
            low=self._low,
            close=self._close,
            volume=self._volume,
        )
