"""Dashboard auth (ROADMAP M16, ADR-026): argon2 password check + a signed
session cookie (Starlette `SessionMiddleware`). Single user, localhost-first
(docs/architecture/06-config-security-ops.md "Dashboard auth").

Page routes (HTML) check `is_authenticated()` directly and redirect to
`/login` themselves — a redirect isn't naturally expressible as a FastAPI
dependency failure. API/websocket routes use `require_login()`, which raises
a flat 401; there's nowhere sensible to redirect a JSON or websocket client.
"""

from __future__ import annotations

import secrets as stdlib_secrets

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError
from fastapi import HTTPException, Request, status
from starlette.requests import HTTPConnection

_hasher = PasswordHasher()

_SESSION_KEY_AUTHENTICATED = "authenticated"
_SESSION_KEY_CSRF = "csrf_token"


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError):
        return False
    return True


def is_authenticated(conn: HTTPConnection) -> bool:
    """`HTTPConnection` (not `Request`) so this also works for a `WebSocket`
    — both are `HTTPConnection` subclasses and `SessionMiddleware` populates
    `.session` on either the same way."""
    return bool(conn.session.get(_SESSION_KEY_AUTHENTICATED))


def log_in(request: Request) -> None:
    request.session[_SESSION_KEY_AUTHENTICATED] = True
    request.session[_SESSION_KEY_CSRF] = stdlib_secrets.token_urlsafe(32)


def log_out(request: Request) -> None:
    request.session.clear()


def csrf_token(request: Request) -> str:
    """The current session's CSRF token, minted on demand if the session
    predates this call (defensive — `log_in` always sets one)."""
    token = request.session.get(_SESSION_KEY_CSRF)
    if not token:
        token = stdlib_secrets.token_urlsafe(32)
        request.session[_SESSION_KEY_CSRF] = token
    return str(token)


def verify_csrf(request: Request, submitted: str) -> bool:
    expected = request.session.get(_SESSION_KEY_CSRF)
    return bool(expected) and stdlib_secrets.compare_digest(str(expected), submitted)


def require_login(request: Request) -> None:
    """FastAPI dependency for API/websocket routes — raises 401, never redirects."""
    if not is_authenticated(request):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="not authenticated")


def require_csrf(request: Request, submitted: str) -> None:
    """Raises 403 on a missing/mismatched CSRF token (ADR-026 decision 4:
    every mutating route must call this before acting)."""
    if not verify_csrf(request, submitted):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="invalid CSRF token")
