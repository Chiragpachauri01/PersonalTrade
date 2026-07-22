"""api/ (ROADMAP M16): auth, CSRF, the REST endpoints, and the websocket —
in-process via FastAPI's TestClient (ASGI transport, no real socket needed).
The Playwright smoke test (tests/test_e2e_dashboard.py) covers the same
flows through a real browser against a real running server.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from personaltrade.api.app import create_app
from personaltrade.api.auth import hash_password
from personaltrade.core.config import AppConfig, Secrets
from personaltrade.core.enums import Interval, Mode, RecommendationAction
from personaltrade.core.errors import ConfigError
from personaltrade.data.store.candles import CandleStore
from personaltrade.data.store.db import build_engine, build_session_factory, session_scope
from personaltrade.data.store.models import Base, Instrument, Position, Recommendation
from personaltrade.data.store.repos import InstrumentRepository
from tests.factories import synthetic_candles

PASSWORD = "correct horse battery staple"


def _secrets(
    *, password: str | None = PASSWORD, session_secret: str | None = "test-secret"
) -> Secrets:
    return Secrets(
        _env_file=None,
        pt_dashboard_password_hash=hash_password(password) if password is not None else None,
        pt_dashboard_session_secret=session_secret,
    )


@pytest.fixture()
def session_factory(tmp_path: Path) -> sessionmaker[Session]:
    engine = build_engine(tmp_path / "test.db")
    Base.metadata.create_all(engine)
    return build_session_factory(engine)


@pytest.fixture()
def candle_store(tmp_path: Path) -> CandleStore:
    return CandleStore(tmp_path / "candles")


@pytest.fixture()
def seeded_instrument(
    session_factory: sessionmaker[Session], candle_store: CandleStore
) -> Instrument:
    with session_scope(session_factory) as session:
        inst = InstrumentRepository(session).add(
            Instrument(
                symbol="AAA", exchange="NSE", instrument_key="NSE_EQ|AAA", tick_size=Decimal("0.05")
            )
        )
        session.flush()
        session.add(
            Position(instrument_id=inst.id, qty=10, avg_price=Decimal("100"), mode=Mode.PAPER)
        )
        session.add(
            Recommendation(
                instrument_id=inst.id,
                action=RecommendationAction.BUY,
                rank=1,
                rationale={"deterministic_action": "BUY", "ai": None},
                created_at=datetime.now(UTC),
            )
        )
        instrument_id = inst.id
    candle_store.write(
        "AAA", "NSE", Interval.M1, synthetic_candles([100.0 + i for i in range(10)])
    )
    with session_scope(session_factory) as session:
        found = InstrumentRepository(session).get(instrument_id)
        assert found is not None
        return found


@pytest.fixture()
def client(
    session_factory: sessionmaker[Session], candle_store: CandleStore
) -> Iterator[TestClient]:
    app = create_app(AppConfig(), _secrets(), session_factory, candle_store)
    with TestClient(app) as test_client:
        yield test_client


def _login(client: TestClient, password: str = PASSWORD) -> None:
    resp = client.post("/login", data={"password": password}, follow_redirects=False)
    assert resp.status_code == 303, resp.text


class TestCreateApp:
    def test_missing_password_hash_raises_config_error(
        self, session_factory: sessionmaker[Session], candle_store: CandleStore
    ) -> None:
        with pytest.raises(ConfigError):
            create_app(
                AppConfig(), _secrets(password=None), session_factory, candle_store
            )

    def test_missing_session_secret_raises_config_error(
        self, session_factory: sessionmaker[Session], candle_store: CandleStore
    ) -> None:
        with pytest.raises(ConfigError):
            create_app(
                AppConfig(), _secrets(session_secret=None), session_factory, candle_store
            )


class TestAuth:
    def test_unauthenticated_overview_redirects_to_login(self, client: TestClient) -> None:
        resp = client.get("/", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/login"

    def test_unauthenticated_api_returns_401(self, client: TestClient) -> None:
        resp = client.get("/api/account")
        assert resp.status_code == 401

    def test_wrong_password_rejected(self, client: TestClient) -> None:
        resp = client.post("/login", data={"password": "nope"})
        assert resp.status_code == 401
        assert "wrong password" in resp.text

    def test_correct_password_logs_in_and_overview_becomes_reachable(
        self, client: TestClient
    ) -> None:
        _login(client)
        resp = client.get("/")
        assert resp.status_code == 200
        assert "Overview" in resp.text

    def test_logout_clears_session(self, client: TestClient) -> None:
        _login(client)
        client.post("/logout")
        resp = client.get("/", follow_redirects=False)
        assert resp.status_code == 303


class TestAccountApi:
    def test_returns_funds_positions_and_kill_switch(
        self, client: TestClient, seeded_instrument: Instrument
    ) -> None:
        _login(client)
        resp = client.get("/api/account")
        assert resp.status_code == 200
        body = resp.json()
        assert body["funds"]["cash"] == "500000"
        assert body["positions"] == [{"symbol": "AAA", "qty": 10, "avg_price": "100"}]
        assert body["kill_switch"] == {
            "tripped": False,
            "reason": None,
            "tripped_at": None,
            "consecutive_errors": 0,
        }


class TestRecommendationsAndJournalApi:
    def test_recommendations_returns_latest_cycle(
        self, client: TestClient, seeded_instrument: Instrument
    ) -> None:
        _login(client)
        resp = client.get("/api/recommendations")
        assert resp.status_code == 200
        recs = resp.json()["recommendations"]
        assert len(recs) == 1
        assert recs[0]["symbol"] == "AAA"
        assert recs[0]["action"] == "BUY"

    def test_journal_empty_with_no_closed_trades(self, client: TestClient) -> None:
        _login(client)
        resp = client.get("/api/journal")
        assert resp.status_code == 200
        assert resp.json() == {"journal": []}


class TestReportsApi:
    def test_daily_report_shape(self, client: TestClient, seeded_instrument: Instrument) -> None:
        _login(client)
        resp = client.get("/api/reports/daily")
        assert resp.status_code == 200
        body = resp.json()
        assert "summary" in body
        assert body["summary"]["closed_trades"] == 0

    def test_weekly_report_shape(self, client: TestClient, seeded_instrument: Instrument) -> None:
        _login(client)
        resp = client.get("/api/reports/weekly")
        assert resp.status_code == 200
        assert "summary" in resp.json()


class TestPages:
    @pytest.mark.parametrize("path", ["/", "/recommendations", "/journal", "/reports"])
    def test_authenticated_pages_render(
        self, client: TestClient, seeded_instrument: Instrument, path: str
    ) -> None:
        _login(client)
        resp = client.get(path)
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    @pytest.mark.parametrize("path", ["/", "/recommendations", "/journal", "/reports"])
    def test_unauthenticated_pages_redirect(self, client: TestClient, path: str) -> None:
        resp = client.get(path, follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/login"


def _csrf_token(client: TestClient) -> str:
    text: str = client.get("/").text
    start = text.index('name="csrf_token" value="') + len('name="csrf_token" value="')
    return text[start : text.index('"', start)]


class TestKillSwitchApi:
    def test_trip_without_csrf_rejected(self, client: TestClient) -> None:
        _login(client)
        resp = client.post("/api/kill-switch/trip", data={"reason": "x"})
        assert resp.status_code == 422  # missing required form field

    def test_trip_with_wrong_csrf_rejected(self, client: TestClient) -> None:
        _login(client)
        resp = client.post(
            "/api/kill-switch/trip", data={"reason": "x", "csrf_token": "wrong"}
        )
        assert resp.status_code == 403

    def test_trip_with_valid_csrf_succeeds(self, client: TestClient) -> None:
        _login(client)
        token = _csrf_token(client)
        resp = client.post(
            "/api/kill-switch/trip", data={"reason": "manual halt", "csrf_token": token}
        )
        assert resp.status_code == 200
        assert resp.json() == {"tripped": True, "reason": "manual halt"}

        account = client.get("/api/account").json()
        assert account["kill_switch"]["tripped"] is True
        assert account["kill_switch"]["reason"] == "manual halt"

    def test_reset_requires_correct_password(self, client: TestClient) -> None:
        _login(client)
        token = _csrf_token(client)
        client.post("/api/kill-switch/trip", data={"reason": "x", "csrf_token": token})

        resp = client.post(
            "/api/kill-switch/reset",
            data={"reason": "resume", "password": "wrong", "csrf_token": token},
        )
        assert resp.status_code == 403
        account = client.get("/api/account").json()
        assert account["kill_switch"]["tripped"] is True  # still tripped

    def test_reset_with_correct_password_succeeds(self, client: TestClient) -> None:
        _login(client)
        token = _csrf_token(client)
        client.post("/api/kill-switch/trip", data={"reason": "x", "csrf_token": token})

        resp = client.post(
            "/api/kill-switch/reset",
            data={"reason": "resume", "password": PASSWORD, "csrf_token": token},
        )
        assert resp.status_code == 200
        assert resp.json() == {"tripped": False, "reason": None}

    def test_reset_when_not_tripped_returns_409(self, client: TestClient) -> None:
        _login(client)
        token = _csrf_token(client)
        resp = client.post(
            "/api/kill-switch/reset",
            data={"reason": "resume", "password": PASSWORD, "csrf_token": token},
        )
        assert resp.status_code == 409


class TestWebsocket:
    def test_unauthenticated_connection_is_closed(self, client: TestClient) -> None:
        with (
            client.websocket_connect("/ws/live") as ws,
            pytest.raises(Exception),  # noqa: B017 - starlette raises WebSocketDisconnect
        ):
            ws.receive_json()

    def test_authenticated_connection_receives_a_snapshot(
        self, client: TestClient, seeded_instrument: Instrument
    ) -> None:
        _login(client)
        with client.websocket_connect("/ws/live") as ws:
            message = ws.receive_json()
        assert message["positions"] == [{"symbol": "AAA", "qty": 10, "avg_price": "100"}]
        assert message["kill_switch"]["tripped"] is False
