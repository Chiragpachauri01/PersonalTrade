"""Upstox market-data implementation (historical + instrument master + live feed).

Historical/instrument endpoints are public — no credentials needed (verified live
2026-07-19):
- instrument master: assets.upstox.com gzipped JSON per exchange
- historical candles: /v3/historical-candle/{key}/{unit}/{interval}/{to}/{from}

Candles arrive newest-first as [ts+05:30, open, high, low, close, volume, oi];
we normalize to the provider-neutral frame contract (UTC, ascending, unique).
Long ranges are fetched in chunks with a polite delay.

`stream_quotes()` (ROADMAP M10) is different in kind: it needs a real Upstox
OAuth access token, and that flow doesn't exist until M17 — so unlike the rest
of this file, its correctness against Upstox's real production servers is
unverified (see ADR-020). What IS verified: the wire protocol, sourced from
Upstox's public docs and vendored schema (data/providers/proto/), decoded via
protobuf round-trip tests, and exercised end-to-end (authorize -> connect ->
subscribe -> decode -> reconnect-on-drop) against a local mock websocket server
speaking that exact protocol (tests/test_data_providers_upstox_stream.py).
"""

from __future__ import annotations

import asyncio
import gzip
import json
import time
from collections.abc import AsyncGenerator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import httpx
import pandas as pd
import websockets
from google.protobuf.message import DecodeError
from websockets.exceptions import WebSocketException

from personaltrade.core.enums import Interval
from personaltrade.core.logging import get_logger
from personaltrade.data.providers.base import (
    InstrumentInfo,
    MarketDataError,
    Quote,
    empty_candle_frame,
    normalize_candle_frame,
)
from personaltrade.data.providers.proto import market_data_feed_v3_pb2 as pb
from personaltrade.data.providers.reconnect import ReconnectPolicy

ASSETS_URL = "https://assets.upstox.com/market-quote/instruments/exchange/{exchange}.json.gz"
HISTORICAL_URL = (
    "https://api.upstox.com/v3/historical-candle/"
    "{instrument_key}/{unit}/{interval}/{to_date}/{from_date}"
)
FEED_AUTHORIZE_URL = "https://api.upstox.com/v3/feed/market-data-feed/authorize"

#: our interval → (upstox unit, upstox interval, max days per request chunk)
_INTERVAL_MAP: dict[Interval, tuple[str, int, int]] = {
    Interval.D1: ("days", 1, 365),
    Interval.M15: ("minutes", 15, 30),
    Interval.M1: ("minutes", 1, 7),
}

_CHUNK_DELAY_SECONDS = 0.3

logger = get_logger(__name__)


class MissingAccessToken(MarketDataError):
    """stream_quotes() needs an Upstox OAuth access token (M17 auth flow); none configured."""


def _decode_feed_response(raw: bytes) -> list[Quote]:
    """One wire message can carry ticks for several instruments (`feeds` is a
    map); mode is always requested as "ltpc" (data/providers/proto/
    market_data_feed_v3.proto — the minimum needed for OHLCV, see Quote's
    docstring), so any other populated oneof is skipped defensively rather
    than guessed at.
    """
    response = pb.FeedResponse()
    response.ParseFromString(raw)
    quotes: list[Quote] = []
    for instrument_key, feed in response.feeds.items():
        if feed.WhichOneof("FeedUnion") != "ltpc":
            continue
        ltpc = feed.ltpc
        quotes.append(
            Quote(
                instrument_key=instrument_key,
                ltp=Decimal(str(ltpc.ltp)),
                ltq=ltpc.ltq,
                # ltt is epoch milliseconds — inferred from the field's int64 type
                # and Upstox's other tick-timestamp fields; unverified against a
                # real feed until M17 (see module docstring).
                ltt=datetime.fromtimestamp(ltpc.ltt / 1000, tz=UTC),
                close=Decimal(str(ltpc.cp)),
            )
        )
    return quotes


class UpstoxMarketData:
    """MarketDataProvider implementation for Upstox: historical + live (ROADMAP M4, M10)."""

    def __init__(self, client: httpx.Client | None = None, access_token: str | None = None) -> None:
        self._client = client or httpx.Client(timeout=60, headers={"Accept": "application/json"})
        self._access_token = access_token

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

    def _authorize_websocket(self) -> str:
        if not self._access_token:
            raise MissingAccessToken(
                "stream_quotes() requires an access token; none configured "
                "(the OAuth flow to obtain one arrives at ROADMAP M17)"
            )
        try:
            response = self._client.get(
                FEED_AUTHORIZE_URL, headers={"Authorization": f"Bearer {self._access_token}"}
            )
            response.raise_for_status()
            payload: dict[str, Any] = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise MarketDataError(f"market-data-feed authorize failed: {exc}") from exc
        try:
            return str(payload["data"]["authorized_redirect_uri"])
        except (KeyError, TypeError) as exc:
            raise MarketDataError(f"malformed authorize response: {payload!r}") from exc

    async def stream_quotes(
        self,
        instrument_keys: list[str],
        *,
        reconnect_policy: ReconnectPolicy | None = None,
        max_reconnect_attempts: int | None = None,
    ) -> AsyncGenerator[Quote, None]:
        """Ticks for `instrument_keys`, reconnecting transparently on any
        transport failure (ROADMAP M10 risk: websocket instability) — callers
        see only a brief gap in ticks, never an exception, unless
        `max_reconnect_attempts` is set and exceeded. Each dropped connection
        re-authorizes (the redirect URI is single-use) before reconnecting.
        """
        policy = reconnect_policy or ReconnectPolicy()
        attempt = 0
        while True:
            try:
                ws_url = self._authorize_websocket()
                async with websockets.connect(ws_url) as ws:
                    await ws.send(
                        json.dumps(
                            {
                                "guid": str(uuid4()),
                                "method": "sub",
                                "data": {"mode": "ltpc", "instrumentKeys": instrument_keys},
                            }
                        ).encode("utf-8")
                    )
                    async for message in ws:
                        attempt = 0  # a message proves the connection is healthy
                        raw = message if isinstance(message, bytes) else message.encode("utf-8")
                        try:
                            quotes = _decode_feed_response(raw)
                        except DecodeError as exc:
                            logger.warning("market_data_feed_decode_error", error=str(exc))
                            continue
                        for quote in quotes:
                            yield quote
            except MissingAccessToken:
                raise  # a config error, not a transient failure — never worth retrying
            except (OSError, WebSocketException, MarketDataError) as exc:
                attempt += 1
                if max_reconnect_attempts is not None and attempt > max_reconnect_attempts:
                    raise MarketDataError(
                        f"market-data-feed: exceeded {max_reconnect_attempts} "
                        f"reconnect attempts: {exc}"
                    ) from exc
                delay = policy.delay_for(attempt - 1)
                logger.warning(
                    "market_data_feed_reconnecting", attempt=attempt, delay=delay, error=str(exc)
                )
                await asyncio.sleep(delay)
