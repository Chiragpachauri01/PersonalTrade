from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect

from personaltrade.data.store.models import Base

REPO_ROOT = Path(__file__).resolve().parents[1]

EXPECTED_TABLES = {
    "instruments",
    "strategy_runs",
    "signals",
    "orders",
    "order_events",
    "trades",
    "positions",
    "risk_events",
    "kill_switch_state",
    "paper_account",
    "ai_analyses",
    "recommendations",
    "news_items",
    "news_instrument_tags",
    "backtest_runs",
    "backtest_trades",
}


def _alembic_config(db_url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


def _db_url(tmp_path: Path) -> str:
    return f"sqlite:///{(tmp_path / 'migrate.db').as_posix()}"


def test_upgrade_head_creates_full_schema(tmp_path: Path) -> None:
    url = _db_url(tmp_path)
    command.upgrade(_alembic_config(url), "head")

    engine = create_engine(url)
    tables = set(inspect(engine).get_table_names())
    engine.dispose()
    assert tables >= EXPECTED_TABLES
    assert "alembic_version" in tables


def test_downgrade_base_removes_schema(tmp_path: Path) -> None:
    url = _db_url(tmp_path)
    cfg = _alembic_config(url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")

    engine = create_engine(url)
    tables = set(inspect(engine).get_table_names())
    engine.dispose()
    assert EXPECTED_TABLES.isdisjoint(tables)


def test_migrations_match_models_no_drift(tmp_path: Path) -> None:
    """Autogenerate diff between migrated schema and ORM metadata must be empty."""
    url = _db_url(tmp_path)
    command.upgrade(_alembic_config(url), "head")

    engine = create_engine(url)
    with engine.connect() as conn:
        ctx = MigrationContext.configure(
            conn, opts={"compare_type": False, "render_as_batch": True}
        )
        diff = compare_metadata(ctx, Base.metadata)
    engine.dispose()
    assert diff == [], f"models and migrations have drifted: {diff}"
