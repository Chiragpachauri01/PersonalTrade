from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

MINIMAL_DEFAULT_YAML = """\
trading:
  mode: paper
  live_orders_enabled: false
  universe: [RELIANCE]
risk:
  capital: "500000"
  risk_per_trade_pct: "1.0"
  max_open_positions: 5
  max_daily_loss_pct: "3.0"
  kill_switch:
    max_consecutive_errors: 5
ai:
  enabled: true
  provider: anthropic
  model: claude-opus-4-8
data:
  candle_root: data/candles
  db_path: data/personaltrade.db
log:
  level: INFO
  format: json
  dir: data/logs
"""


@pytest.fixture()
def config_dir(tmp_path: Path) -> Path:
    """A hermetic config directory containing a valid default.yaml."""
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "default.yaml").write_text(MINIMAL_DEFAULT_YAML, encoding="utf-8")
    return cfg_dir


@pytest.fixture(autouse=True)
def _clean_pt_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Isolate tests from any PT_* variables in the developer's environment."""
    import os

    for key in [k for k in os.environ if k.startswith("PT_")]:
        monkeypatch.delenv(key)
    yield


@pytest.fixture()
def db_session(tmp_path: Path) -> Iterator[Session]:
    """A session on a fresh file-backed SQLite DB with the full schema."""
    from personaltrade.data.store.db import build_engine, build_session_factory
    from personaltrade.data.store.models import Base

    engine = build_engine(tmp_path / "test.db")
    Base.metadata.create_all(engine)
    session = build_session_factory(engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()
