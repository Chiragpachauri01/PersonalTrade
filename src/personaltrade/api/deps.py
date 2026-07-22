"""FastAPI dependency providers — pull the composition root's state
(config, session factory, candle store) off `app.state` per request. See
`app.py::create_app` for where these are set.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import cast

from fastapi import Request
from sqlalchemy.orm import Session, sessionmaker

from personaltrade.core.config import AppConfig, Secrets
from personaltrade.data.store.candles import CandleStore
from personaltrade.data.store.db import session_scope


def get_config(request: Request) -> AppConfig:
    return cast(AppConfig, request.app.state.config)


def get_secrets(request: Request) -> Secrets:
    return cast(Secrets, request.app.state.secrets)


def get_candle_store(request: Request) -> CandleStore:
    return cast(CandleStore, request.app.state.candle_store)


def get_session_factory(request: Request) -> sessionmaker[Session]:
    return cast("sessionmaker[Session]", request.app.state.session_factory)


def get_db(request: Request) -> Iterator[Session]:
    """One session per request — commits on success, rolls back on any
    exception, same unit-of-work contract as the CLI's `session_scope`."""
    factory = get_session_factory(request)
    with session_scope(factory) as session:
        yield session
