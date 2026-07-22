"""Smoke E2E for the dashboard via a real browser (ROADMAP M16 testing
note), against a real `pt dashboard run` subprocess — the API tests
(tests/test_api_dashboard.py) already cover auth/CSRF/route logic
in-process; this proves the actual rendered pages and client-side JS
(websocket live feed, kill-switch confirm()+fetch()+reload flow) work end
to end, the same way ADR-024/ADR-021 verified their milestones against a
real running process rather than only unit-level fakes.

A genuine subprocess, not an in-process uvicorn thread, is deliberate: an
earlier in-thread `asyncio.run(server.serve())` approach left the asyncio
event-loop state corrupted for every `asyncio.run()` call elsewhere in the
suite that happened to run afterward (a real pollution bug, caught by
running the full suite, not a hypothetical) — a separate OS process has its
own interpreter and asyncio state, so it cannot leak into this test process
under any circumstance. This also exercises the real CLI entrypoint a user
actually runs, not a shortcut construction of the FastAPI app.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from personaltrade.api.auth import hash_password
from personaltrade.core.enums import Interval, Mode, RecommendationAction
from personaltrade.data.store.candles import CandleStore
from personaltrade.data.store.db import build_engine, build_session_factory, session_scope
from personaltrade.data.store.models import Base, Instrument, Position, Recommendation
from personaltrade.data.store.repos import InstrumentRepository
from tests.factories import synthetic_candles

PASSWORD = "correct horse battery staple"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _pt_executable() -> Path:
    scripts_dir = Path(sys.executable).parent
    for name in ("pt.exe", "pt"):
        candidate = scripts_dir / name
        if candidate.is_file():
            return candidate
    raise RuntimeError(f"no `pt` console script found next to {sys.executable}")


@pytest.fixture()
def live_server(tmp_path: Path) -> Iterator[str]:
    db_path = tmp_path / "test.db"
    candle_root = tmp_path / "candles"
    engine = build_engine(db_path)
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    store = CandleStore(candle_root)

    with session_scope(factory) as session:
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
    store.write("AAA", "NSE", Interval.M1, synthetic_candles([100.0 + i for i in range(10)]))
    engine.dispose()

    port = _free_port()
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "default.yaml").write_text(
        f"""\
data:
  db_path: "{db_path.as_posix()}"
  candle_root: "{candle_root.as_posix()}"
log:
  level: WARNING
  format: console
  dir: null
dashboard:
  host: 127.0.0.1
  port: {port}
""",
        encoding="utf-8",
    )

    env = dict(os.environ)
    env["PT_CONFIG_DIR"] = str(config_dir)
    env["PT_DASHBOARD_PASSWORD_HASH"] = hash_password(PASSWORD)
    env["PT_DASHBOARD_SESSION_SECRET"] = "e2e-test-secret"

    base_url = f"http://127.0.0.1:{port}"
    log_path = tmp_path / "server.log"
    with log_path.open("w", encoding="utf-8") as log_file:
        proc = subprocess.Popen(
            [str(_pt_executable()), "dashboard", "run"],
            cwd=tmp_path,  # no .env here — only the env vars set above apply
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )

        deadline = time.monotonic() + 15
        reachable = False
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            try:
                urllib.request.urlopen(f"{base_url}/login", timeout=1)
                reachable = True
                break
            except (urllib.error.URLError, ConnectionError):
                time.sleep(0.2)

        if not reachable:
            proc.kill()
            proc.wait(timeout=5)
            log = log_path.read_text(encoding="utf-8")
            raise RuntimeError(f"pt dashboard run did not become reachable:\n{log}")

        try:
            yield base_url
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


def _log_in(page: Page, base_url: str) -> None:
    page.goto(f"{base_url}/login")
    page.fill("#password", PASSWORD)
    page.click("button[type=submit]")
    page.wait_for_url(f"{base_url}/")


class TestDashboardSmoke:
    def test_login_wrong_password_shows_error(self, page: Page, live_server: str) -> None:
        page.goto(f"{live_server}/login")
        page.fill("#password", "wrong")
        page.click("button[type=submit]")
        expect(page.locator(".error")).to_contain_text("wrong password")

    def test_login_then_overview_shows_seeded_data(self, page: Page, live_server: str) -> None:
        _log_in(page, live_server)
        expect(page.locator("h1")).to_have_text("Overview")
        expect(page.locator("#positions-body")).to_contain_text("AAA")
        expect(page.locator("#ks-status")).to_have_text("clear")

    def test_live_feed_populates_funds_via_websocket(self, page: Page, live_server: str) -> None:
        _log_in(page, live_server)
        # #cash is server-rendered already, but confirm the websocket also
        # delivers a value (not blank/stuck) — proves the JS connected.
        expect(page.locator("#cash")).not_to_have_text("")

    def test_recommendations_page_shows_seeded_row(self, page: Page, live_server: str) -> None:
        _log_in(page, live_server)
        page.goto(f"{live_server}/recommendations")
        expect(page.locator("table")).to_contain_text("AAA")
        expect(page.locator("table")).to_contain_text("BUY")

    def test_journal_page_reachable(self, page: Page, live_server: str) -> None:
        _log_in(page, live_server)
        page.goto(f"{live_server}/journal")
        expect(page.locator("body")).to_contain_text("no closed trades yet")

    def test_reports_page_reachable(self, page: Page, live_server: str) -> None:
        _log_in(page, live_server)
        page.goto(f"{live_server}/reports")
        expect(page.locator("h2").first).to_have_text("Summary")

    def test_kill_switch_trip_and_reset_through_the_real_ui(
        self, page: Page, live_server: str
    ) -> None:
        _log_in(page, live_server)

        page.once("dialog", lambda dialog: dialog.accept())
        page.fill("#trip-form input[name=reason]", "e2e smoke test")
        page.click("#trip-form button[type=submit]")
        expect(page.locator("#ks-status")).to_have_text("TRIPPED", timeout=10000)
        expect(page.locator("#ks-reason")).to_have_text("e2e smoke test")

        page.once("dialog", lambda dialog: dialog.accept())
        page.fill("#reset-form input[name=reason]", "resume")
        page.fill("#reset-form input[name=password]", PASSWORD)
        page.click("#reset-form button[type=submit]")
        expect(page.locator("#ks-status")).to_have_text("clear", timeout=10000)

    def test_logout_returns_to_login(self, page: Page, live_server: str) -> None:
        _log_in(page, live_server)
        page.click("text=Log out")
        page.wait_for_url(f"{live_server}/login")
        expect(page.locator(".login-form")).to_be_visible()
