"""FastAPI composition root for the dashboard (ROADMAP M16, ADR-026) —
mirrors `orchestrator/wiring.py`'s role for the trading loop: reads config,
constructs concrete pieces, wires them onto `app.state`, and every route
pulls what it needs from there via `api/deps.py`.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from sqlalchemy.orm import Session, sessionmaker
from starlette.middleware.sessions import SessionMiddleware
from starlette.staticfiles import StaticFiles

from personaltrade.core.config import AppConfig, Secrets
from personaltrade.core.errors import ConfigError
from personaltrade.data.store.candles import CandleStore

_PACKAGE_DIR = Path(__file__).parent


def create_app(
    cfg: AppConfig,
    secrets: Secrets,
    session_factory: sessionmaker[Session],
    candle_store: CandleStore,
) -> FastAPI:
    """Raises `ConfigError` if the dashboard's two required secrets
    (ADR-026 decision 3) aren't set — fails closed, never falls back to a
    baked-in session-signing default (CLAUDE.md Rule 15)."""
    if secrets.pt_dashboard_password_hash is None or secrets.pt_dashboard_session_secret is None:
        raise ConfigError(
            "dashboard requires PT_DASHBOARD_PASSWORD_HASH and PT_DASHBOARD_SESSION_SECRET "
            "in .env — generate both with `pt auth set-password`"
        )

    from personaltrade.api.routes import api as api_routes
    from personaltrade.api.routes import pages as page_routes
    from personaltrade.api.routes import ws as ws_routes

    app = FastAPI(title="PersonalTrade Dashboard")
    app.state.config = cfg
    app.state.secrets = secrets
    app.state.session_factory = session_factory
    app.state.candle_store = candle_store

    app.add_middleware(
        SessionMiddleware,
        secret_key=secrets.pt_dashboard_session_secret.get_secret_value(),
        session_cookie="pt_dashboard_session",
        same_site="lax",
        https_only=False,  # localhost-first (ADR-026); front with TLS/VPN if ever exposed
    )

    app.mount("/static", StaticFiles(directory=_PACKAGE_DIR / "static"), name="static")
    app.include_router(page_routes.router)
    app.include_router(api_routes.router, prefix="/api")
    app.include_router(ws_routes.router)

    return app
