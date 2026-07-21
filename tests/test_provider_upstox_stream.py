"""UpstoxMarketData.stream_quotes(): protobuf decode correctness (round-trip
against the real vendored schema) and the full authorize -> connect -> subscribe
-> decode -> reconnect pipeline against a local mock websocket server speaking
the exact Upstox V3 wire protocol (ROADMAP M10 testing plan: "recorded-stream
replay tests; reconnect chaos tests"). No real Upstox server involved or
needed — see ADR-020 for why that's the honest scope here.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
import websockets
from websockets.asyncio.server import ServerConnection

from personaltrade.data.providers.base import MarketDataError, Quote
from personaltrade.data.providers.proto import market_data_feed_v3_pb2 as pb
from personaltrade.data.providers.reconnect import ReconnectPolicy
from personaltrade.data.providers.upstox import (
    FEED_AUTHORIZE_URL,
    MissingAccessToken,
    UpstoxMarketData,
    _decode_feed_response,
)

FAST_POLICY = ReconnectPolicy(base_delay=0.01, max_delay=0.02)


def _feed_response_bytes(ticks: dict[str, tuple[float, int, int, float]]) -> bytes:
    response = pb.FeedResponse()
    response.type = pb.live_feed
    for key, (ltp, ltt_ms, ltq, cp) in ticks.items():
        feed = response.feeds[key]
        feed.ltpc.ltp = ltp
        feed.ltpc.ltt = ltt_ms
        feed.ltpc.ltq = ltq
        feed.ltpc.cp = cp
        feed.requestMode = pb.ltpc
    return response.SerializeToString()


class TestDecodeFeedResponse:
    def test_decodes_a_single_ltpc_tick(self) -> None:
        raw = _feed_response_bytes({"NSE_EQ|X": (1301.23, 1732000000000, 10, 1300.0)})
        quotes = _decode_feed_response(raw)
        assert len(quotes) == 1
        quote = quotes[0]
        assert quote.instrument_key == "NSE_EQ|X"
        assert quote.ltp == Decimal("1301.23")
        assert quote.ltq == 10
        assert quote.close == Decimal("1300.0")
        assert quote.ltt == datetime.fromtimestamp(1732000000.0, tz=UTC)

    def test_decodes_multiple_instruments_in_one_message(self) -> None:
        raw = _feed_response_bytes(
            {
                "NSE_EQ|A": (100.0, 1732000000000, 5, 99.0),
                "NSE_EQ|B": (200.0, 1732000000000, 7, 198.0),
            }
        )
        quotes = _decode_feed_response(raw)
        assert {q.instrument_key for q in quotes} == {"NSE_EQ|A", "NSE_EQ|B"}

    def test_non_ltpc_feed_is_skipped(self) -> None:
        response = pb.FeedResponse()
        response.type = pb.live_feed
        feed = response.feeds["NSE_EQ|X"]
        feed.fullFeed.marketFF.ltpc.ltp = 100.0  # populates the fullFeed oneof branch, not ltpc
        assert _decode_feed_response(response.SerializeToString()) == []

    def test_empty_feeds_map_returns_empty_list(self) -> None:
        response = pb.FeedResponse()
        response.type = pb.market_info
        assert _decode_feed_response(response.SerializeToString()) == []


class TestAuthorizeWebsocket:
    def test_missing_token_raises_before_any_request(self) -> None:
        provider = UpstoxMarketData(access_token=None)
        with pytest.raises(MissingAccessToken):
            provider._authorize_websocket()

    def test_parses_authorized_redirect_uri(self) -> None:
        def handle(request: httpx.Request) -> httpx.Response:
            assert request.url == FEED_AUTHORIZE_URL
            assert request.headers["authorization"] == "Bearer tok123"
            return httpx.Response(
                200, json={"status": "success", "data": {"authorized_redirect_uri": "wss://x/y"}}
            )

        provider = UpstoxMarketData(
            client=httpx.Client(transport=httpx.MockTransport(handle)), access_token="tok123"
        )
        assert provider._authorize_websocket() == "wss://x/y"

    def test_malformed_response_raises(self) -> None:
        transport = httpx.MockTransport(lambda _: httpx.Response(200, json={"status": "success"}))
        provider = UpstoxMarketData(client=httpx.Client(transport=transport), access_token="tok")
        with pytest.raises(MarketDataError, match="malformed authorize response"):
            provider._authorize_websocket()

    def test_http_error_raises(self) -> None:
        transport = httpx.MockTransport(lambda _: httpx.Response(401, text="unauthorized"))
        provider = UpstoxMarketData(client=httpx.Client(transport=transport), access_token="tok")
        with pytest.raises(MarketDataError, match="authorize failed"):
            provider._authorize_websocket()


def _authorizing_client(ws_url: str) -> httpx.Client:
    return httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, json={"status": "success", "data": {"authorized_redirect_uri": ws_url}}
            )
        )
    )


class TestStreamQuotesAgainstMockServer:
    async def _collect_one(self) -> Quote:
        async def handler(ws: ServerConnection) -> None:
            sub = json.loads(await ws.recv())
            assert sub["method"] == "sub"
            assert sub["data"]["mode"] == "ltpc"
            assert sub["data"]["instrumentKeys"] == ["NSE_EQ|X"]
            await ws.send(_feed_response_bytes({"NSE_EQ|X": (100.0, 1732000000000, 10, 99.0)}))
            await ws.wait_closed()  # hold the connection open until the client disconnects

        async with websockets.serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            provider = UpstoxMarketData(
                client=_authorizing_client(f"ws://127.0.0.1:{port}/feeds"), access_token="tok"
            )
            gen = provider.stream_quotes(["NSE_EQ|X"])
            quote = await anext(gen)
            await gen.aclose()
            return quote

    def test_receives_and_decodes_a_real_tick(self) -> None:
        quote = asyncio.run(asyncio.wait_for(self._collect_one(), timeout=10))
        assert quote.instrument_key == "NSE_EQ|X"
        assert quote.ltp == Decimal("100.0")
        assert quote.ltq == 10

    async def _collect_across_reconnect(self) -> list[Quote]:
        connection_count = 0

        async def handler(ws: ServerConnection) -> None:
            nonlocal connection_count
            connection_count += 1
            await ws.recv()  # subscribe message
            if connection_count == 1:
                await ws.send(_feed_response_bytes({"NSE_EQ|X": (100.0, 1732000000000, 10, 99.0)}))
                await ws.close()  # simulate a dropped connection
            else:
                await ws.send(_feed_response_bytes({"NSE_EQ|X": (105.0, 1732000060000, 8, 99.0)}))
                await ws.wait_closed()  # hold the connection open until the client disconnects

        async with websockets.serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            provider = UpstoxMarketData(
                client=_authorizing_client(f"ws://127.0.0.1:{port}/feeds"), access_token="tok"
            )
            received: list[Quote] = []
            gen = provider.stream_quotes(["NSE_EQ|X"], reconnect_policy=FAST_POLICY)
            async for quote in gen:
                received.append(quote)
                if len(received) == 2:
                    break
            await gen.aclose()
            return received

    def test_reconnects_transparently_after_a_drop(self) -> None:
        received = asyncio.run(asyncio.wait_for(self._collect_across_reconnect(), timeout=10))
        assert [q.ltp for q in received] == [Decimal("100.0"), Decimal("105.0")]

    async def _always_drops(self) -> None:
        async def handler(ws: ServerConnection) -> None:
            await ws.close()  # drop immediately, no data, every connection

        async with websockets.serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            provider = UpstoxMarketData(
                client=_authorizing_client(f"ws://127.0.0.1:{port}/feeds"), access_token="tok"
            )
            gen = provider.stream_quotes(
                ["NSE_EQ|X"], reconnect_policy=FAST_POLICY, max_reconnect_attempts=2
            )
            async for _ in gen:
                pass  # pragma: no cover — never reached

    def test_gives_up_after_max_reconnect_attempts(self) -> None:
        with pytest.raises(MarketDataError, match="exceeded 2 reconnect attempts"):
            asyncio.run(asyncio.wait_for(self._always_drops(), timeout=10))
