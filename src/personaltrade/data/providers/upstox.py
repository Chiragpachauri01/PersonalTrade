"""Upstox market-data implementation (historical + instrument master).

Both surfaces are public — no credentials needed (verified live 2026-07-19):
- instrument master: assets.upstox.com gzipped JSON per exchange
- historical candles: /v3/historical-candle/{key}/{unit}/{interval}/{to}/{from}

Candles arrive newest-first as [ts+05:30, open, high, low, close, volume, oi];
we normalize to the provider-neutral frame contract (UTC, ascending, unique).
Long ranges are fetched in chunks with a polite delay.
"""

from __future__ import annotations

import gzip
import json
import time
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pandas as pd

from personaltrade.core.enums import Interval
from personaltrade.data.providers.base import (
    InstrumentInfo,
    MarketDataError,
    empty_candle_frame,
    normalize_candle_frame,
)

ASSETS_URL = "https://assets.upstox.com/market-quote/instruments/exchange/{exchange}.json.gz"
HISTORICAL_URL = (
    "https://api.upstox.com/v3/historical-candle/"
    "{instrument_key}/{unit}/{interval}/{to_date}/{from_date}"
)

#: our interval → (upstox unit, upstox interval, max days per request chunk)
_INTERVAL_MAP: dict[Interval, tuple[str, int, int]] = {
    Interval.D1: ("days", 1, 365),
    Interval.M15: ("minutes", 15, 30),
    Interval.M1: ("minutes", 1, 7),
}

_CHUNK_DELAY_SECONDS = 0.3


class UpstoxMarketData:
    """MarketDataProvider implementation for Upstox (historical only until M10)."""

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client or httpx.Client(timeout=60, headers={"Accept": "application/json"})

    def get_instruments(self, exchange: str = "NSE") -> list[InstrumentInfo]:
        url = ASSETS_URL.format(exchange=exchange)
        try:
            response = self._client.get(url)
            response.raise_for_status()
            raw = json.loads(gzip.decompress(response.content))
        except (httpx.HTTPError, OSError, ValueError) as exc:
            raise MarketDataError(f"instrument master fetch failed: {exc}") from exc

        instruments = []
        for row in raw:
            if row.get("segment") != f"{exchange}_EQ" or row.get("instrument_type") != "EQ":
                continue
            try:
                instruments.append(
                    InstrumentInfo(
                        symbol=row["trading_symbol"],
                        exchange=exchange,
                        isin=row["isin"],
                        instrument_key=row["instrument_key"],
                        name=row.get("name", ""),
                        # master publishes tick size in paise (e.g. 10.0 == ₹0.10)
                        tick_size=Decimal(str(row["tick_size"])) / 100,
                        lot_size=int(row["lot_size"]),
                    )
                )
            except (KeyError, ValueError) as exc:
                raise MarketDataError(f"malformed instrument row {row!r}: {exc}") from exc
        return instruments

    def get_historical_candles(
        self,
        instrument_key: str,
        interval: Interval,
        from_date: date,
        to_date: date,
    ) -> pd.DataFrame:
        if to_date < from_date:
            raise MarketDataError(f"to_date {to_date} before from_date {from_date}")
        unit, api_interval, chunk_days = _INTERVAL_MAP[interval]

        frames: list[pd.DataFrame] = []
        chunk_end = to_date
        while chunk_end >= from_date:
            chunk_start = max(from_date, chunk_end - timedelta(days=chunk_days - 1))
            frames.append(
                self._fetch_chunk(instrument_key, unit, api_interval, chunk_start, chunk_end)
            )
            chunk_end = chunk_start - timedelta(days=1)
            if chunk_end >= from_date:
                time.sleep(_CHUNK_DELAY_SECONDS)

        combined = pd.concat(frames, ignore_index=True) if frames else empty_candle_frame()
        return normalize_candle_frame(combined)

    def _fetch_chunk(
        self, instrument_key: str, unit: str, api_interval: int, from_date: date, to_date: date
    ) -> pd.DataFrame:
        url = HISTORICAL_URL.format(
            instrument_key=instrument_key,
            unit=unit,
            interval=api_interval,
            to_date=to_date.isoformat(),
            from_date=from_date.isoformat(),
        )
        try:
            response = self._client.get(url)
            response.raise_for_status()
            payload: dict[str, Any] = response.json()
        except httpx.HTTPStatusError as exc:
            raise MarketDataError(
                f"historical candles {instrument_key} {from_date}..{to_date}: "
                f"HTTP {exc.response.status_code}: {exc.response.text[:200]}"
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise MarketDataError(f"historical candles fetch failed: {exc}") from exc

        if payload.get("status") != "success":
            raise MarketDataError(f"upstox returned non-success payload: {payload}")
        candles = payload.get("data", {}).get("candles") or []
        if not candles:
            return empty_candle_frame()
        frame = pd.DataFrame(
            candles, columns=["ts", "open", "high", "low", "close", "volume", "oi"]
        )
        frame["ts"] = pd.to_datetime(frame["ts"], utc=True)
        return frame
