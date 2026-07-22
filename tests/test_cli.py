from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from typer.testing import CliRunner

from personaltrade import __version__
from personaltrade.cli import app
from personaltrade.core.enums import Interval
from personaltrade.data.store.candles import CandleStore
from personaltrade.data.store.db import build_engine, build_session_factory, session_scope
from personaltrade.data.store.models import Instrument
from personaltrade.data.store.repos import InstrumentRepository, UpstoxTokenRepository
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
    def test_run_by_registry_name_persists_and_prints_metrics(self, backtest_env: Path) -> None:
        assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
        _seed_backtest_data(backtest_env)

        result = runner.invoke(
            app,
            [
                "backtest",
                "run",
                "sma_crossover",
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

    def test_dotted_path_escape_hatch_still_works(self, backtest_env: Path) -> None:
        assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
        _seed_backtest_data(backtest_env)

        result = runner.invoke(
            app,
            [
                "backtest",
                "run",
                "personaltrade.strategy.strategies.sma_crossover:SMACrossoverStrategy",
                "AAA",
            ],
        )
        assert result.exit_code == 0, result.output

    def test_unknown_registry_name_rejected(self, backtest_env: Path) -> None:
        assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
        _seed_backtest_data(backtest_env)

        # no colon -> treated as a registry lookup, not a malformed dotted path
        result = runner.invoke(app, ["backtest", "run", "no_such_strategy", "AAA"])
        assert result.exit_code == 1
        assert "unknown strategy" in result.output

    def test_malformed_dotted_path_rejected(self, backtest_env: Path) -> None:
        assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
        _seed_backtest_data(backtest_env)

        # a colon but no class name after it
        result = runner.invoke(app, ["backtest", "run", "some.module:", "AAA"])
        assert result.exit_code == 1
        assert "module:ClassName" in result.output

    def test_unknown_strategy_module_rejected(self, backtest_env: Path) -> None:
        assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
        _seed_backtest_data(backtest_env)

        result = runner.invoke(app, ["backtest", "run", "no.such.module:NoSuchStrategy", "AAA"])
        assert result.exit_code == 1
        assert "could not import module" in result.output

    def test_unknown_symbol_reports_failure(self, backtest_env: Path) -> None:
        assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
        _seed_backtest_data(backtest_env)

        result = runner.invoke(app, ["backtest", "run", "sma_crossover", "NOPE"])
        assert result.exit_code == 1
        assert "backtest FAILED" in result.output

    def test_no_symbols_and_empty_universe_rejected(self, backtest_env: Path) -> None:
        assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
        _seed_backtest_data(backtest_env)

        result = runner.invoke(app, ["backtest", "run", "sma_crossover"])
        assert result.exit_code == 1
        assert "trading.universe is empty" in result.output


def _seed_sweep_data(tmp_path: Path, days: int = 90) -> None:
    """Longer series than _seed_backtest_data: a sweep needs room for both
    an in-sample and an out-of-sample window plus indicator warm-up."""
    db_path = tmp_path / "pt.db"
    candle_root = tmp_path / "candles"
    engine = build_engine(db_path)
    factory = build_session_factory(engine)
    with session_scope(factory) as session:
        InstrumentRepository(session).add(
            Instrument(
                symbol="BBB", exchange="NSE", instrument_key="NSE_EQ|BBB", tick_size=Decimal("0.05")
            )
        )
    engine.dispose()
    opens = [100.0 + (i % 20) for i in range(days)]  # oscillating, gives crossovers both windows
    CandleStore(candle_root).write("BBB", "NSE", Interval.D1, synthetic_candles(opens))


class TestBacktestSweepCLI:
    def test_sweep_prints_in_sample_and_out_of_sample_metrics(self, backtest_env: Path) -> None:
        assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
        _seed_sweep_data(backtest_env)

        result = runner.invoke(
            app,
            [
                "backtest",
                "sweep",
                "sma_crossover",
                "BBB",
                "--interval",
                "1d",
                "--from",
                "2026-01-01",
                "--to",
                "2026-04-01",
                "--grid",
                '{"fast_period": [3, 5], "slow_period": [10]}',
                "--oos-fraction",
                "0.3",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "sweeping 2 combination(s)" in result.output
        assert "IS[" in result.output
        assert "OOS[" in result.output

    def test_sweep_reports_invalid_combo_without_aborting(self, backtest_env: Path) -> None:
        assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
        _seed_sweep_data(backtest_env)

        result = runner.invoke(
            app,
            [
                "backtest",
                "sweep",
                "sma_crossover",
                "BBB",
                "--from",
                "2026-01-01",
                "--to",
                "2026-04-01",
                "--grid",
                '{"fast_period": [5, 30], "slow_period": [10]}',  # 30 >= 10 is invalid
            ],
        )
        assert result.exit_code == 0, result.output
        assert "ERROR" in result.output
        assert "IS[" in result.output  # the valid combo still ran

    def test_sweep_unknown_strategy_rejected(self, backtest_env: Path) -> None:
        assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
        _seed_sweep_data(backtest_env)

        result = runner.invoke(app, ["backtest", "sweep", "no_such_strategy", "BBB"])
        assert result.exit_code == 1
        assert "unknown strategy" in result.output


class TestStrategyListCLI:
    def test_lists_all_registered_strategies(self, backtest_env: Path) -> None:
        result = runner.invoke(app, ["strategy", "list"])
        assert result.exit_code == 0
        assert "sma_crossover" in result.output
        assert "ema_atr_stop" in result.output
        assert "rsi_mean_reversion" in result.output


class TestPaperCLI:
    def test_status_on_empty_account(self, backtest_env: Path) -> None:
        assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0

        result = runner.invoke(app, ["paper", "status"])
        assert result.exit_code == 0, result.output
        assert "cash=₹500000" in result.output  # risk.capital default
        assert "no open positions" in result.output

    def test_market_order_fills_and_status_shows_position(self, backtest_env: Path) -> None:
        assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
        _seed_backtest_data(backtest_env)

        order_result = runner.invoke(app, ["paper", "order", "AAA", "BUY", "10"])
        assert order_result.exit_code == 0, order_result.output
        assert "state=FILLED" in order_result.output

        status_result = runner.invoke(app, ["paper", "status"])
        assert status_result.exit_code == 0
        assert "AAA: qty=10" in status_result.output

    def test_limit_order_not_marketable_stays_open(self, backtest_env: Path) -> None:
        assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
        _seed_backtest_data(backtest_env)  # last close = 113

        result = runner.invoke(
            app,
            ["paper", "order", "AAA", "BUY", "10", "--type", "limit", "--price", "50"],
        )
        assert result.exit_code == 0, result.output
        assert "state=OPEN" in result.output

    def test_limit_order_without_price_rejected(self, backtest_env: Path) -> None:
        assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
        _seed_backtest_data(backtest_env)

        result = runner.invoke(app, ["paper", "order", "AAA", "BUY", "10", "--type", "limit"])
        assert result.exit_code == 1
        assert "--price is required" in result.output

    def test_invalid_side_rejected(self, backtest_env: Path) -> None:
        assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
        _seed_backtest_data(backtest_env)

        result = runner.invoke(app, ["paper", "order", "AAA", "SIDEWAYS", "10"])
        assert result.exit_code == 1
        assert "invalid side" in result.output

    def test_unknown_symbol_rejected(self, backtest_env: Path) -> None:
        assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
        _seed_backtest_data(backtest_env)

        result = runner.invoke(app, ["paper", "order", "NOPE", "BUY", "10"])
        assert result.exit_code == 1
        assert "not in instruments table" in result.output


class TestReportCLI:
    def test_daily_report_with_no_trades(self, backtest_env: Path) -> None:
        assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0

        result = runner.invoke(app, ["report", "daily"])
        assert result.exit_code == 0, result.output
        assert "closed_trades=0" in result.output
        assert "journal: no closed trades in this period" in result.output

    def test_weekly_report_with_no_trades(self, backtest_env: Path) -> None:
        assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0

        result = runner.invoke(app, ["report", "weekly"])
        assert result.exit_code == 0, result.output
        assert "closed_trades=0" in result.output

    def test_daily_report_after_round_trip_shows_journal_and_breakdowns(
        self, backtest_env: Path
    ) -> None:
        assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
        _seed_backtest_data(backtest_env)  # last close = 113

        assert runner.invoke(app, ["paper", "order", "AAA", "BUY", "10"]).exit_code == 0
        assert runner.invoke(app, ["paper", "order", "AAA", "SELL", "10"]).exit_code == 0

        result = runner.invoke(app, ["report", "daily"])
        assert result.exit_code == 0, result.output
        assert "closed_trades=1" in result.output
        assert "by instrument:" in result.output
        assert "AAA:" in result.output
        assert "journal (closed trades):" in result.output
        assert "AAA BUY qty=10" in result.output


class TestNewsCLI:
    def test_sync_with_no_sources_configured_fails(self, backtest_env: Path) -> None:
        assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
        (backtest_env / "config" / "local.yaml").write_text(
            "news:\n  sources: []\n", encoding="utf-8"
        )

        result = runner.invoke(app, ["news", "sync"])
        assert result.exit_code == 1
        assert "no news sources configured" in result.output

    def test_list_with_no_news_says_so(self, backtest_env: Path) -> None:
        assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
        _seed_backtest_data(backtest_env)

        result = runner.invoke(app, ["news", "list", "AAA"])
        assert result.exit_code == 0, result.output
        assert "no news for AAA" in result.output

    def test_list_unknown_symbol_rejected(self, backtest_env: Path) -> None:
        assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0

        result = runner.invoke(app, ["news", "list", "NOPE"])
        assert result.exit_code == 1
        assert "not in instruments table" in result.output

    def test_list_shows_tagged_news(self, backtest_env: Path) -> None:
        assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
        _seed_backtest_data(backtest_env)

        from personaltrade.data.store.db import build_engine, build_session_factory, session_scope
        from personaltrade.data.store.models import NewsInstrumentTag, NewsItem
        from personaltrade.data.store.repos import InstrumentRepository, NewsRepository

        db_path = backtest_env / "pt.db"
        engine = build_engine(db_path)
        factory = build_session_factory(engine)
        with session_scope(factory) as session:
            inst = InstrumentRepository(session).get_by_symbol("AAA", "NSE")
            assert inst is not None
            item = NewsRepository(session).add_if_new(
                NewsItem(source="rss", url="https://ex.com/aaa-1", title="AAA rallies on results")
            )
            assert item is not None
            session.add(NewsInstrumentTag(news_item_id=item.id, instrument_id=inst.id))
        engine.dispose()

        result = runner.invoke(app, ["news", "list", "AAA"])
        assert result.exit_code == 0, result.output
        assert "AAA rallies on results" in result.output
        assert "https://ex.com/aaa-1" in result.output


class TestAnalyzeCLI:
    def test_ai_disabled_rejected(self, backtest_env: Path) -> None:
        assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
        _seed_backtest_data(backtest_env)
        (backtest_env / "config" / "local.yaml").write_text(
            "ai:\n  enabled: false\n", encoding="utf-8"
        )

        result = runner.invoke(app, ["analyze", "AAA"])
        assert result.exit_code == 1
        assert "AI analysis is disabled" in result.output

    def test_unknown_symbol_rejected(self, backtest_env: Path) -> None:
        assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
        _seed_backtest_data(backtest_env)

        result = runner.invoke(app, ["analyze", "NOPE"])
        assert result.exit_code == 1
        assert "not in instruments table" in result.output

    def test_no_stored_candles_rejected(self, backtest_env: Path) -> None:
        assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
        engine = build_engine(backtest_env / "pt.db")
        factory = build_session_factory(engine)
        with session_scope(factory) as session:
            InstrumentRepository(session).add(
                Instrument(
                    symbol="AAA",
                    exchange="NSE",
                    instrument_key="NSE_EQ|AAA",
                    tick_size=Decimal("0.05"),
                )
            )
        engine.dispose()

        result = runner.invoke(app, ["analyze", "AAA"])
        assert result.exit_code == 1
        assert "no stored candles" in result.output

    def test_no_backend_configured_rejected(
        self, backtest_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
        _seed_backtest_data(backtest_env)
        # Secrets() reads ./.env relative to CWD — chdir so the repo's own
        # .env (which may hold real credentials for manual testing) can't
        # leak into this test (see TestDataStreamCLI.test_no_access_token_rejected).
        monkeypatch.chdir(backtest_env)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)

        result = runner.invoke(app, ["analyze", "AAA"])
        assert result.exit_code == 1
        assert "no AI backend configured" in result.output

    def test_budget_exhausted_reported(
        self, backtest_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")
        assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
        _seed_backtest_data(backtest_env)
        (backtest_env / "config" / "local.yaml").write_text(
            "ai:\n  daily_call_cap: 0\n", encoding="utf-8"
        )

        result = runner.invoke(app, ["analyze", "AAA"])
        assert result.exit_code == 1
        assert "AI budget exhausted" in result.output

    def test_successful_analysis_prints_and_persists_audit_row(
        self, backtest_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from personaltrade.data.store.repos import AIAnalysisRepository
        from personaltrade.intelligence.analysis.schema import AIAnalysisOutput
        from personaltrade.intelligence.llm.provider import LLMResult

        monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")
        assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
        _seed_backtest_data(backtest_env)

        class _FakeProvider:
            def analyze(self, **kwargs: object) -> LLMResult[AIAnalysisOutput]:
                return LLMResult(
                    parsed=AIAnalysisOutput(
                        stance="bullish",
                        conviction="high",
                        key_factors=["steady demand"],
                        risks=["input costs"],
                        news_impact="none",
                        summary="Holding up well into the print.",
                    ),
                    raw_text="raw",
                    model="claude-opus-4-8",
                    input_tokens=10,
                    output_tokens=5,
                    cost_usd=Decimal("0.001"),
                )

        monkeypatch.setattr(
            "personaltrade.intelligence.llm.anthropic_provider.build_anthropic_provider",
            lambda secrets, ai_cfg: _FakeProvider(),
        )

        result = runner.invoke(app, ["analyze", "AAA"])
        assert result.exit_code == 0, result.output
        assert "AAA: bullish (conviction=high)" in result.output
        assert "Holding up well into the print." in result.output
        assert "steady demand" in result.output
        assert "cost=$0.001" in result.output

        engine = build_engine(backtest_env / "pt.db")
        factory = build_session_factory(engine)
        with session_scope(factory) as session:
            rows = AIAnalysisRepository(session).list_all()
            assert len(rows) == 1
            assert rows[0].output["stance"] == "bullish"
        engine.dispose()


class TestRecommendRunCLI:
    def _seed(self, tmp_path: Path) -> None:
        """AAA candles with a genuine SMA(2)/SMA(4) golden cross on the final
        bar (hand-verified — same series used by
        test_intelligence_recommendation_screener.py), so `sma_crossover`
        with small periods emits a real LONG signal, not a scripted one."""
        db_path = tmp_path / "pt.db"
        candle_root = tmp_path / "candles"
        engine = build_engine(db_path)
        factory = build_session_factory(engine)
        with session_scope(factory) as session:
            InstrumentRepository(session).add(
                Instrument(
                    symbol="AAA",
                    exchange="NSE",
                    instrument_key="NSE_EQ|AAA",
                    tick_size=Decimal("0.05"),
                )
            )
        engine.dispose()
        CandleStore(candle_root).write(
            "AAA", "NSE", Interval.D1, synthetic_candles([100, 99, 98, 97, 96, 95, 150])
        )

    def _write_local_yaml(self, backtest_env: Path, extra: str = "") -> None:
        (backtest_env / "config" / "local.yaml").write_text(
            "trading:\n"
            "  universe: [AAA]\n"
            "  strategy_params:\n"
            "    fast_period: 2\n"
            "    slow_period: 4\n" + extra,
            encoding="utf-8",
        )

    def test_empty_universe_rejected(self, backtest_env: Path) -> None:
        assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0

        result = runner.invoke(app, ["recommend", "run"])
        assert result.exit_code == 1
        assert "trading.universe is empty" in result.output

    def test_no_stored_candles_rejected(self, backtest_env: Path) -> None:
        assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
        (backtest_env / "config" / "local.yaml").write_text(
            "trading:\n  universe: [NOPE]\nai:\n  enabled: false\n", encoding="utf-8"
        )

        result = runner.invoke(app, ["recommend", "run"])
        assert result.exit_code == 1
        assert "nothing to screen" in result.output

    def test_ai_disabled_still_produces_deterministic_recommendation(
        self, backtest_env: Path
    ) -> None:
        assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
        self._seed(backtest_env)
        self._write_local_yaml(backtest_env, extra="ai:\n  enabled: false\n")

        result = runner.invoke(app, ["recommend", "run"])
        assert result.exit_code == 0, result.output
        assert "deterministic-only" in result.output
        assert "#1 AAA: BUY (ai=unavailable)" in result.output

        engine = build_engine(backtest_env / "pt.db")
        factory = build_session_factory(engine)
        with session_scope(factory) as session:
            from personaltrade.data.store.repos import RecommendationRepository

            rows = RecommendationRepository(session).list_all()
            assert len(rows) == 1
            assert rows[0].action.value == "BUY"
            assert rows[0].ai_analysis_id is None
        engine.dispose()

    def test_no_backend_configured_degrades_to_deterministic_only(
        self, backtest_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
        self._seed(backtest_env)
        self._write_local_yaml(backtest_env)
        monkeypatch.chdir(backtest_env)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)

        result = runner.invoke(app, ["recommend", "run"])
        assert result.exit_code == 0, result.output
        assert "deterministic-only" in result.output
        assert "#1 AAA: BUY (ai=unavailable)" in result.output

    def test_successful_ai_merge_persists_audit_row(
        self, backtest_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from personaltrade.data.store.repos import AIAnalysisRepository, RecommendationRepository
        from personaltrade.intelligence.analysis.schema import AIAnalysisOutput
        from personaltrade.intelligence.llm.provider import LLMResult

        assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
        self._seed(backtest_env)
        self._write_local_yaml(backtest_env)

        class _FakeProvider:
            def analyze(self, **kwargs: object) -> LLMResult[AIAnalysisOutput]:
                return LLMResult(
                    parsed=AIAnalysisOutput(
                        stance="bearish",
                        conviction="high",
                        key_factors=["weak demand"],
                        risks=["margins"],
                        news_impact="negative",
                        summary="Bad print just landed.",
                    ),
                    raw_text="raw",
                    model="claude-opus-4-8",
                    input_tokens=10,
                    output_tokens=5,
                    cost_usd=Decimal("0.002"),
                )

        monkeypatch.setattr(
            "personaltrade.intelligence.llm.anthropic_provider.build_anthropic_provider",
            lambda secrets, ai_cfg: _FakeProvider(),
        )
        monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")

        result = runner.invoke(app, ["recommend", "run"])
        assert result.exit_code == 0, result.output
        assert "#1 AAA: HOLD (ai=bearish/high)" in result.output

        engine = build_engine(backtest_env / "pt.db")
        factory = build_session_factory(engine)
        with session_scope(factory) as session:
            recs = RecommendationRepository(session).list_all()
            assert len(recs) == 1
            assert recs[0].action.value == "HOLD"
            assert recs[0].ai_analysis_id is not None
            assert "veto" in recs[0].rationale
            assert AIAnalysisRepository(session).list_all()[0].output["stance"] == "bearish"
        engine.dispose()


class TestRiskKillSwitchCLI:
    def test_status_starts_clear(self, backtest_env: Path) -> None:
        assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0

        result = runner.invoke(app, ["risk", "kill-switch", "status"])
        assert result.exit_code == 0, result.output
        assert "clear" in result.output
        assert "consecutive_errors=0" in result.output

    def test_trip_then_status_shows_tripped(self, backtest_env: Path) -> None:
        assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0

        trip_result = runner.invoke(
            app, ["risk", "kill-switch", "trip", "--reason", "manual halt for testing"]
        )
        assert trip_result.exit_code == 0, trip_result.output
        assert "TRIPPED" in trip_result.output

        status_result = runner.invoke(app, ["risk", "kill-switch", "status"])
        assert status_result.exit_code == 0
        assert "TRIPPED" in status_result.output
        assert "manual halt for testing" in status_result.output

    def test_reset_without_trip_fails(self, backtest_env: Path) -> None:
        assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0

        result = runner.invoke(app, ["risk", "kill-switch", "reset", "--reason", "n/a"])
        assert result.exit_code == 1
        assert "not tripped" in result.output

    def test_trip_then_reset_clears_it(self, backtest_env: Path) -> None:
        assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
        assert (
            runner.invoke(app, ["risk", "kill-switch", "trip", "--reason", "halt"]).exit_code == 0
        )

        reset_result = runner.invoke(
            app, ["risk", "kill-switch", "reset", "--reason", "reviewed, resuming"]
        )
        assert reset_result.exit_code == 0, reset_result.output
        assert "reset" in reset_result.output.lower()

        status_result = runner.invoke(app, ["risk", "kill-switch", "status"])
        assert "clear" in status_result.output


def _seed_holidays_file(backtest_env: Path) -> None:
    (backtest_env / "config" / "nse_holidays.yaml").write_text("holidays: {}\n", encoding="utf-8")


class TestDataStreamCLI:
    def test_no_access_token_rejected(
        self, backtest_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
        _seed_backtest_data(backtest_env)
        _seed_holidays_file(backtest_env)
        # Secrets() reads ./.env relative to CWD, independent of PT_CONFIG_DIR —
        # chdir (after the setup above, which needs the repo's real CWD for
        # alembic's relative script_location) so the repo's own .env (which may
        # have a real token for manual testing) can't leak into this test.
        monkeypatch.chdir(backtest_env)
        monkeypatch.delenv("UPSTOX_ACCESS_TOKEN", raising=False)

        result = runner.invoke(app, ["data", "stream", "AAA"])
        assert result.exit_code == 1
        assert "no Upstox access token configured" in result.output

    def test_daily_interval_rejected(
        self, backtest_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("UPSTOX_ACCESS_TOKEN", "fake-token")
        assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
        _seed_backtest_data(backtest_env)
        _seed_holidays_file(backtest_env)

        result = runner.invoke(app, ["data", "stream", "AAA", "--interval", "1d"])
        assert result.exit_code == 1
        assert "1m/15m" in result.output

    def test_no_symbols_and_empty_universe_rejected(
        self, backtest_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("UPSTOX_ACCESS_TOKEN", "fake-token")
        assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
        _seed_backtest_data(backtest_env)
        _seed_holidays_file(backtest_env)

        result = runner.invoke(app, ["data", "stream"])
        assert result.exit_code == 1
        assert "trading.universe is empty" in result.output

    def test_unknown_symbol_rejected(
        self, backtest_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("UPSTOX_ACCESS_TOKEN", "fake-token")
        assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
        _seed_backtest_data(backtest_env)
        _seed_holidays_file(backtest_env)

        result = runner.invoke(app, ["data", "stream", "NOPE"])
        assert result.exit_code == 1
        assert "not in instruments table" in result.output

    def test_market_closed_reports_and_exits_cleanly(
        self, backtest_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("UPSTOX_ACCESS_TOKEN", "fake-token")
        monkeypatch.setattr(
            "personaltrade.core.calendar.NSECalendar.is_open_at", lambda self, ts: False
        )
        assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
        _seed_backtest_data(backtest_env)
        _seed_holidays_file(backtest_env)

        result = runner.invoke(app, ["data", "stream", "AAA"])
        assert result.exit_code == 0, result.output
        assert "market is closed" in result.output


def _configure_trading(
    backtest_env: Path, *, universe: list[str], strategy: str = "sma_crossover"
) -> None:
    """Appends a trading: section to backtest_env's default.yaml — pt run has
    no --symbols/--strategy CLI args, it reads config.trading exclusively."""
    yaml_path = backtest_env / "config" / "default.yaml"
    universe_yaml = "[" + ", ".join(universe) + "]"
    with yaml_path.open("a", encoding="utf-8") as f:
        f.write(f"\ntrading:\n  mode: paper\n  universe: {universe_yaml}\n  strategy: {strategy}\n")


class TestRunCLI:
    def test_mode_mismatch_with_config_rejected(
        self, backtest_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("UPSTOX_ACCESS_TOKEN", "fake-token")
        _configure_trading(backtest_env, universe=["AAA"])
        assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0

        result = runner.invoke(app, ["run", "--mode", "live"])
        assert result.exit_code == 1
        assert "does not match trading.mode" in result.output

    def test_live_mode_without_stored_token_rejected(
        self, backtest_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ROADMAP M17: live mode needs a `pt auth upstox-login`-obtained
        token in the DB — the old manual UPSTOX_ACCESS_TOKEN stopgap is
        paper-mode-only now (it still works for the market-data feed there,
        but live trading needs the real OAuth-obtained, encrypted token)."""
        yaml_path = backtest_env / "config" / "default.yaml"
        with yaml_path.open("a", encoding="utf-8") as f:
            f.write("\ntrading:\n  mode: live\n  universe: [AAA]\n")
        assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
        # Secrets() reads ./.env relative to CWD — chdir (after db upgrade,
        # which needs the repo's real CWD for alembic's relative
        # script_location) so the repo's own .env (real credentials) can't
        # leak into this test (see TestDataStreamCLI.test_no_access_token_rejected).
        monkeypatch.chdir(backtest_env)
        monkeypatch.setenv("UPSTOX_ACCESS_TOKEN", "fake-token")
        monkeypatch.setenv("PT_TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())

        result = runner.invoke(app, ["run", "--mode", "live"])
        assert result.exit_code == 1
        assert "no Upstox token stored" in result.output

    def test_live_mode_without_encryption_key_rejected(
        self, backtest_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        yaml_path = backtest_env / "config" / "default.yaml"
        with yaml_path.open("a", encoding="utf-8") as f:
            f.write("\ntrading:\n  mode: live\n  universe: [AAA]\n")
        assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
        monkeypatch.chdir(backtest_env)
        monkeypatch.delenv("PT_TOKEN_ENCRYPTION_KEY", raising=False)

        result = runner.invoke(app, ["run", "--mode", "live"])
        assert result.exit_code == 1
        assert "PT_TOKEN_ENCRYPTION_KEY not set" in result.output

    def test_live_mode_with_expired_token_rejected(
        self, backtest_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        yaml_path = backtest_env / "config" / "default.yaml"
        with yaml_path.open("a", encoding="utf-8") as f:
            f.write("\ntrading:\n  mode: live\n  universe: [AAA]\n")
        assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
        monkeypatch.chdir(backtest_env)
        key = Fernet.generate_key()
        monkeypatch.setenv("PT_TOKEN_ENCRYPTION_KEY", key.decode())

        engine = build_engine(backtest_env / "pt.db")
        factory = build_session_factory(engine)
        with session_scope(factory) as session:
            expired = datetime.now(UTC) - timedelta(hours=1)
            UpstoxTokenRepository(session).save(
                Fernet(key).encrypt(b"expired-token").decode(), expired, expired
            )
        engine.dispose()

        result = runner.invoke(app, ["run", "--mode", "live"])
        assert result.exit_code == 1
        assert "token expired" in result.output

    def test_no_access_token_rejected(
        self, backtest_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _configure_trading(backtest_env, universe=["AAA"])
        assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
        monkeypatch.chdir(backtest_env)  # see test_no_access_token_rejected in TestDataStreamCLI
        monkeypatch.delenv("UPSTOX_ACCESS_TOKEN", raising=False)

        result = runner.invoke(app, ["run", "--mode", "paper"])
        assert result.exit_code == 1
        assert "no Upstox access token configured" in result.output

    def test_empty_universe_rejected(
        self, backtest_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("UPSTOX_ACCESS_TOKEN", "fake-token")
        assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0

        result = runner.invoke(app, ["run", "--mode", "paper"])
        assert result.exit_code == 1
        assert "trading.universe is empty" in result.output

    def test_missing_calendar_rejected(
        self, backtest_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("UPSTOX_ACCESS_TOKEN", "fake-token")
        _configure_trading(backtest_env, universe=["AAA"])
        assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0

        result = runner.invoke(app, ["run", "--mode", "paper"])
        assert result.exit_code == 1
        assert "NSE holiday calendar unavailable" in result.output

    def test_unknown_strategy_rejected(
        self, backtest_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("UPSTOX_ACCESS_TOKEN", "fake-token")
        _configure_trading(backtest_env, universe=["AAA"], strategy="no_such_strategy")
        _seed_holidays_file(backtest_env)
        assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0

        result = runner.invoke(app, ["run", "--mode", "paper"])
        assert result.exit_code == 1
        assert "unknown strategy" in result.output

    def test_unknown_symbol_rejected(
        self, backtest_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("UPSTOX_ACCESS_TOKEN", "fake-token")
        _configure_trading(backtest_env, universe=["NOPE"])
        _seed_holidays_file(backtest_env)
        assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0

        result = runner.invoke(app, ["run", "--mode", "paper"])
        assert result.exit_code == 1
        assert "not in instruments table" in result.output
