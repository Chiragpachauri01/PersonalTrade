"""execution/upstox/auth.py: the OAuth login flow (ROADMAP M17, ADR-027).
`wait_for_callback` is exercised against a real local socket (a genuine GET
request, not a mock) since it IS the thing under test — everything else
(token exchange) uses httpx.MockTransport, the established pattern
(tests/test_provider_upstox.py).
"""

from __future__ import annotations

import threading
import time
from typing import cast
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
import pytest

from personaltrade.execution.upstox.auth import (
    UpstoxAuthError,
    build_authorization_url,
    exchange_code_for_token,
    login,
    wait_for_callback,
)

REDIRECT_URI = "http://127.0.0.1:18765/auth/callback"


class TestBuildAuthorizationUrl:
    def test_contains_required_query_params(self) -> None:
        url = build_authorization_url("my-api-key", REDIRECT_URI, "state123")
        parsed = urlparse(url)
        assert parsed.scheme == "https"
        assert parsed.netloc == "api.upstox.com"
        assert parsed.path == "/v2/login/authorization/dialog"
        query = parse_qs(parsed.query)
        assert query["response_type"] == ["code"]
        assert query["client_id"] == ["my-api-key"]
        assert query["redirect_uri"] == [REDIRECT_URI]
        assert query["state"] == ["state123"]


def _hit_callback(url: str, params: dict[str, str], delay: float = 0.1) -> None:
    def _do() -> None:
        time.sleep(delay)
        httpx.get(f"{url}?{urlencode(params)}", timeout=5)

    threading.Thread(target=_do, daemon=True).start()


class TestWaitForCallback:
    def test_captures_code_and_state(self) -> None:
        _hit_callback(REDIRECT_URI, {"code": "auth-code-123", "state": "abc"})
        result = wait_for_callback(REDIRECT_URI, timeout_seconds=5)
        assert result.code == "auth-code-123"
        assert result.state == "abc"
        assert result.error is None

    def test_captures_error(self) -> None:
        _hit_callback(REDIRECT_URI, {"error": "access_denied", "state": "abc"})
        result = wait_for_callback(REDIRECT_URI, timeout_seconds=5)
        assert result.error == "access_denied"
        assert result.code is None

    def test_times_out_with_no_callback(self) -> None:
        with pytest.raises(UpstoxAuthError, match="no callback received"):
            wait_for_callback(REDIRECT_URI, timeout_seconds=0.3)

    def test_rejects_non_localhost_redirect_uri(self) -> None:
        with pytest.raises(UpstoxAuthError, match="must be a localhost URL"):
            wait_for_callback("http://example.com/callback", timeout_seconds=1)


class TestExchangeCodeForToken:
    def test_posts_form_encoded_and_returns_access_token(self) -> None:
        captured: dict[str, object] = {}

        def handle(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["content_type"] = request.headers.get("content-type", "")
            captured["body"] = parse_qs(request.content.decode())
            return httpx.Response(200, json={"access_token": "real-token-xyz"})

        client = httpx.Client(transport=httpx.MockTransport(handle))
        token = exchange_code_for_token(
            client,
            api_key="key",
            api_secret="secret",
            redirect_uri=REDIRECT_URI,
            code="the-code",
        )
        assert token == "real-token-xyz"
        assert captured["url"] == "https://api.upstox.com/v2/login/authorization/token"
        assert "application/x-www-form-urlencoded" in str(captured["content_type"])
        body = cast("dict[str, list[str]]", captured["body"])
        assert body["code"] == ["the-code"]
        assert body["client_id"] == ["key"]
        assert body["client_secret"] == ["secret"]
        assert body["grant_type"] == ["authorization_code"]

    def test_http_error_raises_upstox_auth_error(self) -> None:
        def handle(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"error": "invalid_grant"})

        client = httpx.Client(transport=httpx.MockTransport(handle))
        with pytest.raises(UpstoxAuthError, match="rejected"):
            exchange_code_for_token(
                client, api_key="key", api_secret="secret", redirect_uri=REDIRECT_URI, code="bad"
            )

    def test_missing_access_token_field_raises(self) -> None:
        def handle(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"user_id": "HD1234"})

        client = httpx.Client(transport=httpx.MockTransport(handle))
        with pytest.raises(UpstoxAuthError, match="malformed"):
            exchange_code_for_token(
                client, api_key="key", api_secret="secret", redirect_uri=REDIRECT_URI, code="c"
            )


class TestLoginFlow:
    def test_full_flow_returns_access_token(self) -> None:
        def handle(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"access_token": "final-token"})

        client = httpx.Client(transport=httpx.MockTransport(handle))
        urls: list[str] = []

        # The state token is generated inside login(), unknown to us in
        # advance — capture it via the on_authorization_url callback instead
        # of pre-scripting the callback hit.
        def on_url(url: str) -> None:
            urls.append(url)
            query = parse_qs(urlparse(url).query)
            _hit_callback(REDIRECT_URI, {"code": "the-code", "state": query["state"][0]})

        token = login(
            client,
            api_key="key",
            api_secret="secret",
            redirect_uri=REDIRECT_URI,
            open_browser=False,
            timeout_seconds=5,
            on_authorization_url=on_url,
        )
        assert token == "final-token"
        assert len(urls) == 1

    def test_denied_authorization_raises(self) -> None:
        client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200)))

        def on_url(url: str) -> None:
            query = parse_qs(urlparse(url).query)
            _hit_callback(REDIRECT_URI, {"error": "access_denied", "state": query["state"][0]})

        with pytest.raises(UpstoxAuthError, match="denied"):
            login(
                client,
                api_key="key",
                api_secret="secret",
                redirect_uri=REDIRECT_URI,
                open_browser=False,
                timeout_seconds=5,
                on_authorization_url=on_url,
            )

    def test_mismatched_state_raises(self) -> None:
        client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200)))

        def on_url(url: str) -> None:
            _hit_callback(REDIRECT_URI, {"code": "c", "state": "not-the-real-state"})

        with pytest.raises(UpstoxAuthError, match="state did not match"):
            login(
                client,
                api_key="key",
                api_secret="secret",
                redirect_uri=REDIRECT_URI,
                open_browser=False,
                timeout_seconds=5,
                on_authorization_url=on_url,
            )
