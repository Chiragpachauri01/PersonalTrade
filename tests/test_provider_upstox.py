from __future__ import annotations

import gzip
import json
from datetime import UTC, date
from decimal import Decimal

import httpx
import pandas as pd
import pytest

from personaltrade.core.enums import Interval
from personaltrade.data.providers.base import CANDLE_COLUMNS, MarketDataError
from personaltrade.data.providers.upstox import UpstoxMarketData
from tests.factories import RELIANCE_DAILY_CANDLES, wire_candles_payload

MASTER_ROWS = [
    {
        "segment": "NSE_EQ",
        "name": "RELIANCE INDUSTRIES LTD",
        "exchange": "NSE",
        "isin": "INE002A01018",
        "instrument_type": "EQ",
        "instrument_key": "NSE_EQ|INE002A01018",
        "lot_size": 1,
        "exchange_token": "2885",
        "tick_size": 10.0,
        "trading_symbol": "RELIANCE",
    },
    {  # non-equity row must be filtered out
        "segment": "NSE_INDEX",
        "name": "Nifty 50",
        "exchange": "NSE",
        "instrument_type": "INDEX",
        "instrument_key": "NSE_INDEX|Nifty 50",
        "tick_size": 0.0,
        "trading_symbol": "NIFTY 50",
    },
    {
        "segment": "NSE_EQ",
        "name": "INFOSYS LIMITED",
        "exchange": "NSE",
        "isin": "INE009A01021",
        "instrument_type": "EQ",
        "instrument_key": "NSE_EQ|INE009A01021",
        "lot_size": 1,
        "exchange_token": "1594",
        "tick_size": 5.0,
        "trading_symbol": "INFY",
    },
]


def _provider(handler: httpx.MockTransport) -> UpstoxMarketData:
    return UpstoxMarketData(client=httpx.Client(transport=handler))


def _master_transport() -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        assert "assets.upstox.com" in str(request.url)
        return httpx.Response(200, content=gzip.compress(json.dumps(MASTER_ROWS).encode()))

    return httpx.MockTransport(handle)


class TestInstrumentMaster:
    def test_parses_and_filters_equities(self) -> None:
        instruments = _provider(_master_transport()).get_instruments()
        assert [i.symbol for i in instruments] == ["RELIANCE", "INFY"]
        rel = instruments[0]
        assert rel.instrument_key == "NSE_EQ|INE002A01018"
        assert rel.isin == "INE002A01018"
        assert rel.tick_size == Decimal("0.1")  # paise -> rupees
        assert instruments[1].tick_size == Decimal("0.05")

    def test_http_error_wrapped(self) -> None:
        transport = httpx.MockTransport(lambda _: httpx.Response(503))
        with pytest.raises(MarketDataError, match="instrument master"):
            _provider(transport).get_instruments()


class TestHistoricalCandles:
    def test_normalizes_wire_candles(self) -> None:
        def handle(request: httpx.Request) -> httpx.Response:
            assert "/v3/historical-candle/NSE_EQ|INE002A01018/days/1/" in str(request.url)
            return httpx.Response(200, json=wire_candles_payload(RELIANCE_DAILY_CANDLES))

        frame = _provider(httpx.MockTransport(handle)).get_historical_candles(
            "NSE_EQ|INE002A01018", Interval.D1, date(2026, 7, 1), date(2026, 7, 17)
        )
        assert list(frame.columns) == CANDLE_COLUMNS
        assert len(frame) == 13
        # wire order is newest-first with IST offsets; frame must be ascending UTC
        assert frame["ts"].is_monotonic_increasing
        assert frame["ts"].iloc[0].tzinfo is not None
        assert frame["ts"].iloc[0] == pd_ts("2026-06-30 18:30:00")
        assert frame["close"].iloc[-1] == 1327.2
        assert frame["volume"].dtype == "int64"

    def test_chunks_long_ranges_and_merges(self) -> None:
        seen_ranges = []

        def handle(request: httpx.Request) -> httpx.Response:
            parts = request.url.path.split("/")
            to_d, from_d = parts[-2], parts[-1]
            seen_ranges.append((from_d, to_d))
            # return one candle per chunk, dated at the chunk end
            candle = [[f"{to_d}T00:00:00+05:30", 100.0, 101.0, 99.0, 100.5, 1000, 0]]
            return httpx.Response(200, json=wire_candles_payload(candle))

        # M1 chunk size is 7 days; 10-day range must produce 2 requests
        frame = _provider(httpx.MockTransport(handle)).get_historical_candles(
            "NSE_EQ|X", Interval.M1, date(2026, 7, 1), date(2026, 7, 10)
        )
        assert len(seen_ranges) == 2
        assert seen_ranges[0] == ("2026-07-04", "2026-07-10")  # newest chunk first
        assert seen_ranges[1] == ("2026-07-01", "2026-07-03")
        assert len(frame) == 2
        assert frame["ts"].is_monotonic_increasing

    def test_non_success_payload_rejected(self) -> None:
        transport = httpx.MockTransport(
            lambda _: httpx.Response(200, json={"status": "error", "errors": ["boom"]})
        )
        with pytest.raises(MarketDataError, match="non-success"):
            _provider(transport).get_historical_candles(
                "NSE_EQ|X", Interval.D1, date(2026, 7, 1), date(2026, 7, 2)
            )

    def test_http_error_includes_status(self) -> None:
        transport = httpx.MockTransport(lambda _: httpx.Response(429, text="rate limited"))
        with pytest.raises(MarketDataError, match="HTTP 429"):
            _provider(transport).get_historical_candles(
                "NSE_EQ|X", Interval.D1, date(2026, 7, 1), date(2026, 7, 2)
            )

    def test_reversed_range_rejected(self) -> None:
        with pytest.raises(MarketDataError, match="before from_date"):
            _provider(_master_transport()).get_historical_candles(
                "NSE_EQ|X", Interval.D1, date(2026, 7, 10), date(2026, 7, 1)
            )


def pd_ts(value: str) -> pd.Timestamp:
    return pd.Timestamp(value, tz=UTC)
