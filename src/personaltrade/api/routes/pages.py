"""HTML pages (ROADMAP M16): the daily-workflow screens — overview
(positions/funds/kill switch), recommendations, journal, reports. Each route
checks `auth.is_authenticated()` directly and redirects to `/login` itself —
a redirect isn't naturally expressible as a FastAPI dependency failure
(see api/auth.py's module docstring).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from personaltrade.analytics.journal import build_journal
from personaltrade.analytics.reports import generate_report
from personaltrade.api import auth, viewmodels
from personaltrade.api.deps import get_candle_store, get_config, get_db, get_secrets
from personaltrade.core.calendar import ist_midnight_utc, ist_week_start_utc
from personaltrade.core.config import AppConfig, Secrets
from personaltrade.core.enums import Mode
from personaltrade.data.store.candles import CandleStore

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).parent.parent / "templates")


def _redirect_to_login() -> RedirectResponse:
    return RedirectResponse("/login", status_code=303)


@router.get("/login", response_class=HTMLResponse, response_model=None)
def login_form(request: Request) -> HTMLResponse | RedirectResponse:
    if auth.is_authenticated(request):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login", response_model=None)
def login_submit(
    request: Request,
    secrets: Annotated[Secrets, Depends(get_secrets)],
    password: Annotated[str, Form()],
) -> HTMLResponse | RedirectResponse:
    password_hash = secrets.pt_dashboard_password_hash
    if password_hash is None or not auth.verify_password(
        password_hash.get_secret_value(), password
    ):
        return templates.TemplateResponse(
            request, "login.html", {"error": "wrong password"}, status_code=401
        )
    auth.log_in(request)
    return RedirectResponse("/", status_code=303)


@router.post("/logout")
def logout(request: Request) -> RedirectResponse:
    auth.log_out(request)
    return RedirectResponse("/login", status_code=303)


@router.get("/", response_class=HTMLResponse, response_model=None)
def overview(
    request: Request,
    cfg: Annotated[AppConfig, Depends(get_config)],
    session: Annotated[Session, Depends(get_db)],
    store: Annotated[CandleStore, Depends(get_candle_store)],
) -> HTMLResponse | RedirectResponse:
    if not auth.is_authenticated(request):
        return _redirect_to_login()
    snapshot = viewmodels.account_snapshot(cfg, session, store)
    return templates.TemplateResponse(
        request,
        "overview.html",
        {"snapshot": snapshot, "csrf_token": auth.csrf_token(request)},
    )


@router.get("/recommendations", response_class=HTMLResponse, response_model=None)
def recommendations_page(
    request: Request, session: Annotated[Session, Depends(get_db)]
) -> HTMLResponse | RedirectResponse:
    if not auth.is_authenticated(request):
        return _redirect_to_login()
    rows = viewmodels.latest_recommendations(session)
    return templates.TemplateResponse(request, "recommendations.html", {"recommendations": rows})


@router.get("/journal", response_class=HTMLResponse, response_model=None)
def journal_page(
    request: Request, session: Annotated[Session, Depends(get_db)]
) -> HTMLResponse | RedirectResponse:
    if not auth.is_authenticated(request):
        return _redirect_to_login()
    entries = build_journal(session, Mode.PAPER)
    return templates.TemplateResponse(request, "journal.html", {"journal": entries})


@router.get("/reports", response_class=HTMLResponse, response_model=None)
def reports_page(
    request: Request,
    cfg: Annotated[AppConfig, Depends(get_config)],
    session: Annotated[Session, Depends(get_db)],
    store: Annotated[CandleStore, Depends(get_candle_store)],
    period: Literal["daily", "weekly"] = "daily",
) -> HTMLResponse | RedirectResponse:
    if not auth.is_authenticated(request):
        return _redirect_to_login()
    now = datetime.now(UTC)
    since = ist_midnight_utc(now) if period == "daily" else ist_week_start_utc(now)
    report = generate_report(
        session,
        store,
        mode=Mode.PAPER,
        initial_cash=cfg.risk.capital,
        interval=viewmodels.default_report_interval(cfg),
        since=since,
    )
    return templates.TemplateResponse(
        request, "reports.html", {"report": report, "period": period}
    )
