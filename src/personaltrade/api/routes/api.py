"""JSON REST API (ROADMAP M16): read-only over the same domain code the CLI
calls, plus the two kill-switch mutations (ADR-026 decision 5 — nothing else
here can place, size, or cancel an order). Every route requires login
(`auth.require_login`); the two POST routes additionally require a valid
CSRF token, and reset additionally re-verifies the password
(docs/architecture/06-config-security-ops.md "Dashboard auth").
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from sqlalchemy.orm import Session

from personaltrade.analytics.journal import build_journal
from personaltrade.analytics.reports import generate_report
from personaltrade.api import auth, viewmodels
from personaltrade.api.deps import get_candle_store, get_config, get_db, get_secrets
from personaltrade.core.calendar import ist_midnight_utc, ist_week_start_utc
from personaltrade.core.config import AppConfig, Secrets
from personaltrade.core.enums import Mode
from personaltrade.data.store.candles import CandleStore
from personaltrade.risk.kill_switch import KillSwitch, KillSwitchNotTripped

router = APIRouter(dependencies=[Depends(auth.require_login)])


@router.get("/account")
def get_account(
    cfg: Annotated[AppConfig, Depends(get_config)],
    session: Annotated[Session, Depends(get_db)],
    store: Annotated[CandleStore, Depends(get_candle_store)],
) -> dict[str, object]:
    snapshot = viewmodels.account_snapshot(cfg, session, store)
    return viewmodels.account_snapshot_json(snapshot)


@router.get("/recommendations")
def get_recommendations(session: Annotated[Session, Depends(get_db)]) -> dict[str, object]:
    rows = viewmodels.latest_recommendations(session)
    return {"recommendations": [viewmodels.recommendation_row_json(r) for r in rows]}


@router.get("/journal")
def get_journal(session: Annotated[Session, Depends(get_db)]) -> dict[str, object]:
    entries = build_journal(session, Mode.PAPER)
    return {"journal": [viewmodels.journal_entry_json(e) for e in entries]}


def _report(
    session: Session, store: CandleStore, cfg: AppConfig, since: datetime
) -> dict[str, object]:
    report = generate_report(
        session,
        store,
        mode=Mode.PAPER,
        initial_cash=cfg.risk.capital,
        interval=viewmodels.default_report_interval(cfg),
        since=since,
    )
    return viewmodels.report_json(report)


@router.get("/reports/daily")
def get_report_daily(
    cfg: Annotated[AppConfig, Depends(get_config)],
    session: Annotated[Session, Depends(get_db)],
    store: Annotated[CandleStore, Depends(get_candle_store)],
) -> dict[str, object]:
    return _report(session, store, cfg, ist_midnight_utc(datetime.now(UTC)))


@router.get("/reports/weekly")
def get_report_weekly(
    cfg: Annotated[AppConfig, Depends(get_config)],
    session: Annotated[Session, Depends(get_db)],
    store: Annotated[CandleStore, Depends(get_candle_store)],
) -> dict[str, object]:
    return _report(session, store, cfg, ist_week_start_utc(datetime.now(UTC)))


@router.post("/kill-switch/trip")
def post_kill_switch_trip(
    request: Request,
    session: Annotated[Session, Depends(get_db)],
    reason: Annotated[str, Form()],
    csrf_token: Annotated[str, Form()],
) -> dict[str, object]:
    auth.require_csrf(request, csrf_token)
    state = KillSwitch(session).trip(reason)
    return {"tripped": state.tripped, "reason": state.reason}


@router.post("/kill-switch/reset")
def post_kill_switch_reset(
    request: Request,
    session: Annotated[Session, Depends(get_db)],
    secrets: Annotated[Secrets, Depends(get_secrets)],
    reason: Annotated[str, Form()],
    password: Annotated[str, Form()],
    csrf_token: Annotated[str, Form()],
) -> dict[str, object]:
    auth.require_csrf(request, csrf_token)
    password_hash = secrets.pt_dashboard_password_hash
    if password_hash is None or not auth.verify_password(
        password_hash.get_secret_value(), password
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="wrong password")
    try:
        state = KillSwitch(session).reset(reason)
    except KillSwitchNotTripped as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return {"tripped": state.tripped, "reason": state.reason}
