from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from personaltrade.core.enums import Interval
from personaltrade.core.events import CandleReceived
from personaltrade.data.providers.base import CANDLE_COLUMNS
from personaltrade.orchestrator.candle_buffer import LiveCandleBuffer


def _candle(ts: datetime, close: str) -> CandleReceived:
    return CandleReceived(
        instrument_key="NSE_EQ|X",
        interval=Interval.M1,
        ts=ts,
        open=Decimal(close),
        high=Decimal(close),
        low=Decimal(close),
        close=Decimal(close),
        volume=10,
    )


class TestLiveCandleBuffer:
    def test_empty_buffer_returns_empty_frame(self) -> None:
        buf = LiveCandleBuffer()
        frame = buf.frame()
        assert frame.empty
        assert list(frame.columns) == CANDLE_COLUMNS

    def test_append_grows_frame_in_order(self) -> None:
        buf = LiveCandleBuffer()
        buf.append(_candle(datetime(2026, 1, 1, 9, 15, tzinfo=UTC), "100"))
        buf.append(_candle(datetime(2026, 1, 1, 9, 16, tzinfo=UTC), "101"))
        assert len(buf) == 2
        frame = buf.frame()
        assert list(frame["close"]) == [100.0, 101.0]
        assert frame["ts"].is_monotonic_increasing
