"""Live updates (ROADMAP M16, ADR-026 decision 2): `/ws/live` polls the DB on
`dashboard.poll_interval_seconds` and pushes an account snapshot (funds,
positions, kill switch) to every connected browser tab — deliberately not
wired to `core.events.EventBus`, since the dashboard must stay useful with no
trading session running at all.
"""

from __future__ import annotations

import asyncio
from typing import cast

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session, sessionmaker
from starlette.websockets import WebSocketState

from personaltrade.api import auth, viewmodels
from personaltrade.core.config import AppConfig
from personaltrade.core.logging import get_logger
from personaltrade.data.store.candles import CandleStore
from personaltrade.data.store.db import session_scope

router = APIRouter()
logger = get_logger(__name__)


@router.websocket("/ws/live")
async def ws_live(websocket: WebSocket) -> None:
    await websocket.accept()
    if not auth.is_authenticated(websocket):
        await websocket.close(code=4401)
        return

    state = websocket.app.state
    cfg = cast(AppConfig, state.config)
    store = cast(CandleStore, state.candle_store)
    factory = cast("sessionmaker[Session]", state.session_factory)
    poll_interval = cfg.dashboard.poll_interval_seconds

    try:
        while websocket.application_state == WebSocketState.CONNECTED:
            with session_scope(factory) as session:
                snapshot = viewmodels.account_snapshot(cfg, session, store)
            await websocket.send_json(viewmodels.account_snapshot_json(snapshot))
            await asyncio.sleep(poll_interval)
    except WebSocketDisconnect:
        pass
