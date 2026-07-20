from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from personaltrade import __version__
from personaltrade.cli import app
from personaltrade.core.enums import Interval
from personaltrade.data.store.candles import CandleStore
from personaltrade.data.store.db import build_engine, build_session_factory, session_scope
from personaltrade.data.store.models import Instrument
from personaltrade.data.store.repos import InstrumentRepository
from tests.factories import synthetic_candles

runner = CliRunner()


def test_version_command() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert f"personaltrade {__version__}" in result.output


def test_version_flag() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "personaltrade" in result.output


def test_config_validate_ok(config_dir: Path) -> None:
    result = runner.invoke(app, ["config", "validate", "--config-dir", str(config_dir)])
    assert result.exit_code == 0
    assert "config OK" in result.output
    assert "mode=paper" in result.output


def test_config_validate_invalid(config_dir: Path) -> None:
    (config_dir / "local.yaml").write_text("trading:\n  mode: yolo\n", encoding="utf-8")
    result = runner.invoke(app, ["config", "validate", "--config-dir", str(config_dir)])
    assert result.exit_code == 1


def test_config_show_outputs_json_without_secrets(config_dir: Path) -> None:
    result = runner.invoke(app, ["config", "show", "--config-dir", str(config_dir)])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["trading"]["mode"] == "paper"
    assert payload["ai"]["model"] == "claude-opus-4-8"
    # secrets never appear in config output
    assert "api_key" not in result.output.lower()


@pytest.fixture()
def backtest_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A config dir + PT_CONFIG_DIR pointing db_path/candle_root at tmp_path.

    Other CLI tests only exercise config load/validate, which never touch
    data.db_path or data.candle_root — this is the first CLI test that writes
    through those paths, so they must be absolute tmp_path locations, never
    the relative "data/..." defaults (which would resolve against the repo's
    real working directory).
    """
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    db_path = (tmp_path / "pt.db").as_posix()
    candle_root = (tmp_path / "candles").as_posix()
    (cfg_dir / "default.yaml").write_text(
        f"""\
data:
  db_path: "{db_path}"
  candle_root: "{candle_root}"
log:
  level: INFO
  format: json
  dir: null
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("PT_CONFIG_DIR", str(cfg_dir))
    return tmp_path


def _seed_backtest_data(tmp_path: Path) -> None:
    db_path = tmp_path / "pt.db"
    candle_root = tmp_path / "candles"
    engine = build_engine(db_path)
    factory = build_session_factory(engine)
    with session_scope(factory) as session:
        InstrumentRepository(session).add(
            Instrument(
                symbol="AAA", exchange="NSE", instrument_key="NSE_EQ|AAA", tick_size=Decimal("0.05")
            )
        )
    engine.dispose()
    CandleStore(candle_root).write(
        "AAA", "NSE", Interval.D1, synthetic_candles([100, 102, 104, 106, 108, 110, 112])
    )


class TestBacktestRunCLI:
    def test_run_persists_and_prints_metrics(self, backtest_env: Path) -> None:
        assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
        _seed_backtest_data(backtest_env)

        result = runner.invoke(
            app,
            [
                "backtest",
                "run",
                "personaltrade.strategy.examples:SMACrossoverStrategy",
                "AAA",
                "--interval",
                "1d",
                "--from",
                "2026-01-01",
                "--to",
                "2026-01-10",
                "--params",
                '{"fast_period": 2, "slow_period": 4}',
            ],
        )
        assert result.exit_code == 0, result.output
        assert "backtest_run_id=" in result.output
        assert "CAGR=" in result.output
        assert "AAA:" in result.output

    def test_malformed_strategy_path_rejected(self, backtest_env: Path) -> None:
        assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
        _seed_backtest_data(backtest_env)

        result = runner.invoke(app, ["backtest", "run", "not_a_module_colon_class", "AAA"])
        assert result.exit_code == 1
        assert "module:ClassName" in result.output

    def test_unknown_strategy_module_rejected(self, backtest_env: Path) -> None:
        assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
        _seed_backtest_data(backtest_env)

        result = runner.invoke(app, ["backtest", "run", "no.such.module:NoSuchStrategy", "AAA"])
        assert result.exit_code == 1
        assert "could not load strategy" in result.output

    def test_unknown_symbol_reports_failure(self, backtest_env: Path) -> None:
        assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
        _seed_backtest_data(backtest_env)

        result = runner.invoke(
            app,
            [
                "backtest",
                "run",
                "personaltrade.strategy.examples:SMACrossoverStrategy",
                "NOPE",
            ],
        )
        assert result.exit_code == 1
        assert "backtest FAILED" in result.output

    def test_no_symbols_and_empty_universe_rejected(self, backtest_env: Path) -> None:
        assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
        _seed_backtest_data(backtest_env)

        result = runner.invoke(
            app, ["backtest", "run", "personaltrade.strategy.examples:SMACrossoverStrategy"]
        )
        assert result.exit_code == 1
        assert "trading.universe is empty" in result.output
