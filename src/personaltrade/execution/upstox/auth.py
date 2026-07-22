"""Upstox OAuth login flow (ROADMAP M17, ADR-027). Endpoints and field names
verified directly against Upstox's own public API documentation (2026-07-22,
see ADR-027) rather than assumed from memory — a wrong field name here would
silently fail every login, and a wrong response field would misparse a real
access token.

The access token itself is never logged or printed; only the authorization
URL and high-level flow status are.
"""

from __future__ import annotations

import secrets as stdlib_secrets
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from personaltrade.core.errors import PersonalTradeError

AUTHORIZE_URL = "https://api.upstox.com/v2/login/authorization/dialog"
TOKEN_URL = "https://api.upstox.com/v2/login/authorization/token"

_CALLBACK_HTML = (
    b"<!doctype html><html><body>"
    b"<p>Login complete &mdash; you can close this tab and return to the terminal.</p>"
    b"</body></html>"
)


class UpstoxAuthError(PersonalTradeError):
    """The OAuth flow failed: user denied, callback timed out/mismatched, or
    token exchange was rejected."""


def build_authorization_url(api_key: str, redirect_uri: str, state: str) -> str:
    params = {
        "response_type": "code",
        "client_id": api_key,
        "redirect_uri": redirect_uri,
        "state": state,
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


@dataclass(frozen=True)
class CallbackResult:
    code: str | None
    state: str | None
    error: str | None


def _make_handler(result: dict[str, CallbackResult]) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            query = parse_qs(urlparse(self.path).query)

            def first(name: str) -> str | None:
                values = query.get(name)
                return values[0] if values else None

            result["value"] = CallbackResult(
                code=first("code"), state=first("state"), error=first("error")
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(_CALLBACK_HTML)

        def log_message(self, format: str, *args: Any) -> None:
            pass  # silence BaseHTTPRequestHandler's default stderr access log

    return Handler


def wait_for_callback(redirect_uri: str, timeout_seconds: float) -> CallbackResult:
    """Blocks until exactly one GET request hits `redirect_uri` (Upstox's
    redirect after login) or `timeout_seconds` elapses. `redirect_uri` must
    be a `http://localhost:PORT/path` URL — the OAuth flow's whole premise
    (docs/architecture/06-config-security-ops.md) is a one-shot local
    listener, not a running server.
    """
    parsed = urlparse(redirect_uri)
    if parsed.hostname not in ("localhost", "127.0.0.1"):
        raise UpstoxAuthError(
            f"redirect_uri {redirect_uri!r} must be a localhost URL for this local callback flow"
        )
    result: dict[str, CallbackResult] = {}
    server = HTTPServer((parsed.hostname, parsed.port or 80), _make_handler(result))
    server.timeout = timeout_seconds
    try:
        server.handle_request()  # exactly one request, or a timeout with no exception
    finally:
        server.server_close()

    if "value" not in result:
        raise UpstoxAuthError(f"no callback received within {timeout_seconds}s")
    return result["value"]


def exchange_code_for_token(
    client: httpx.Client, *, api_key: str, api_secret: str, redirect_uri: str, code: str
) -> str:
    try:
        response = client.post(
            TOKEN_URL,
            data={
                "code": code,
                "client_id": api_key,
                "client_secret": api_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
            headers={"Accept": "application/json"},
        )
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
    except httpx.HTTPStatusError as exc:
        raise UpstoxAuthError(
            f"token exchange rejected: HTTP {exc.response.status_code}: {exc.response.text[:300]}"
        ) from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise UpstoxAuthError(f"token exchange failed: {exc}") from exc
    try:
        return str(payload["access_token"])
    except (KeyError, TypeError) as exc:
        raise UpstoxAuthError(f"malformed token response (no access_token): {payload!r}") from exc


def login(
    client: httpx.Client,
    *,
    api_key: str,
    api_secret: str,
    redirect_uri: str,
    open_browser: bool = True,
    timeout_seconds: float = 180,
    on_authorization_url: Callable[[str], None] | None = None,
) -> str:
    """The full flow: build the authorization URL, open it (unless disabled),
    block for the localhost redirect, exchange the code. Returns the
    plaintext access token — the caller encrypts it before persisting
    (execution/upstox/crypto.py); this function never touches storage.
    """
    state = stdlib_secrets.token_urlsafe(16)
    url = build_authorization_url(api_key, redirect_uri, state)
    if on_authorization_url is not None:
        on_authorization_url(url)
    if open_browser:
        webbrowser.open(url)

    callback = wait_for_callback(redirect_uri, timeout_seconds)
    if callback.error:
        raise UpstoxAuthError(f"Upstox denied authorization: {callback.error}")
    if callback.state != state:
        raise UpstoxAuthError("callback state did not match the request — possible CSRF, aborting")
    if not callback.code:
        raise UpstoxAuthError("callback had no authorization code")

    return exchange_code_for_token(
        client,
        api_key=api_key,
        api_secret=api_secret,
        redirect_uri=redirect_uri,
        code=callback.code,
    )
