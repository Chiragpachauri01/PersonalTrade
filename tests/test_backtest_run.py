from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from personaltrade.backtest.run import NoDataForSymbol, run_backtest_for_symbols
from personaltrade.core.config import BacktestConfig, CostConfig
from personaltrade.core.enums import Interval
from personaltrade.core.enums import SignalDirection as Dir
from personaltrade.data.store.candles import CandleStore
from personaltrade.data.store.models import BacktestRun, BacktestTrade, Instrument
from personaltrade.data.store.repos import InstrumentRepository
from personaltrade.strategy.base import Signal
from tests.factories import LeakyOnceStrategy, ScriptedStrategy, synthetic_candles

FROM = date(2026, 1, 1)
TO = date(2026, 1, 10)


def _seed_instrument(session: Session, symbol: str, store: CandleStore, opens: list[float]) -> None:
    InstrumentRepository(session).add(
        Instrument(
            symbol=symbol,
            exchange="NSE",
            instrument_key=f"NSE_EQ|{symbol}",
            tick_size=Decimal("0.05"),
        )
    )
    session.flush()
    store.write(symbol, "NSE", Interval.D1, synthetic_candles(opens, start=None))


# LONG@0 -> fills@1(open=102); EXIT@2 -> fills@3(open=106): a clean winning round trip.
WINNING_SCHEDULE = {
    0: Signal(Dir.LONG, ref_price=101.0),
    2: Signal(Dir.EXIT, ref_price=105.0),
}


@pytest.fixture()
def store(tmp_path: Path) -> CandleStore:
    return CandleStore(tmp_path / "candles")


class TestNoDataForSymbol:
    def test_unknown_symbol_rejected(self, db_session: Session, store: CandleStore) -> None:
        with pytest.raises(NoDataForSymbol, match="not in instruments table"):
            run_backtest_for_symbols(
                ScriptedStrategy({}),
                ["NOPE"],
                Interval.D1,
                FROM,
                TO,
                session=db_session,
                candle_store=store,
                initial_capital=Decimal("100000"),
                risk_per_trade_pct=Decimal("10"),
                cost_rates=CostConfig(),
                backtest_cfg=BacktestConfig(),
            )

    def test_no_candles_for_range_rejected(self, db_session: Session, store: CandleStore) -> None:
        InstrumentRepository(db_session).add(
            Instrument(
                symbol="EMPTY",
                exchange="NSE",
                instrument_key="NSE_EQ|EMPTY",
                tick_size=Decimal("0.05"),
            )
        )
        db_session.flush()
        with pytest.raises(NoDataForSymbol, match="no stored"):
            run_backtest_for_symbols(
                ScriptedStrategy({}),
                ["EMPTY"],
                Interval.D1,
                FROM,
                TO,
                session=db_session,
                candle_store=store,
                initial_capital=Decimal("100000"),
                risk_per_trade_pct=Decimal("10"),
                cost_rates=CostConfig(),
                backtest_cfg=BacktestConfig(),
            )

    def test_empty_symbol_list_rejected(self, db_session: Session, store: CandleStore) -> None:
        with pytest.raises(NoDataForSymbol, match="no symbols"):
            run_backtest_for_symbols(
                ScriptedStrategy({}),
                [],
                Interval.D1,
                FROM,
                TO,
                session=db_session,
                candle_store=store,
                initial_capital=Decimal("100000"),
                risk_per_trade_pct=Decimal("10"),
                cost_rates=CostConfig(),
                backtest_cfg=BacktestConfig(),
            )


class TestSingleSymbolPersistence:
    def test_persists_run_and_trades(self, db_session: Session, store: CandleStore) -> None:
        _seed_instrument(db_session, "AAA", store, [100, 102, 104, 106, 108, 110, 112])

        result = run_backtest_for_symbols(
            ScriptedStrategy(WINNING_SCHEDULE),
            ["AAA"],
            Interval.D1,
            FROM,
            TO,
            session=db_session,
            candle_store=store,
            initial_capital=Decimal("100000"),
            risk_per_trade_pct=Decimal("10"),
            cost_rates=CostConfig(),
            backtest_cfg=BacktestConfig(),
        )
        db_session.commit()

        run_row = db_session.get(BacktestRun, result.backtest_run_id)
        assert run_row is not None
        assert run_row.strategy_name == "scripted"
        assert run_row.from_date == FROM
        assert run_row.to_date == TO
        assert run_row.data_fingerprint is not None and len(run_row.data_fingerprint) == 64
        assert "portfolio" in run_row.metrics
        assert "per_symbol" in run_row.metrics
        assert "AAA" in run_row.metrics["per_symbol"]
        assert run_row.cost_model_version == CostConfig().model_dump(mode="json")

        trades = db_session.query(BacktestTrade).filter_by(backtest_run_id=run_row.id).all()
        assert len(trades) == 2
        sides = {t.detail["side"] for t in trades}
        assert sides == {"BUY", "SELL"}
        assert all("costs" in t.detail for t in trades)
        assert result.portfolio_metrics.closed_trades == 1
        assert result.portfolio_metrics.win_rate == 1.0

    def test_fingerprint_is_deterministic_across_calls(
        self, db_session: Session, store: CandleStore
    ) -> None:
        _seed_instrument(db_session, "BBB", store, [100, 102, 104, 106, 108, 110, 112])
        r1 = run_backtest_for_symbols(
            ScriptedStrategy(WINNING_SCHEDULE),
            ["BBB"],
            Interval.D1,
            FROM,
            TO,
            session=db_session,
            candle_store=store,
            initial_capital=Decimal("100000"),
            risk_per_trade_pct=Decimal("10"),
            cost_rates=CostConfig(),
            backtest_cfg=BacktestConfig(),
        )
        r2 = run_backtest_for_symbols(
            ScriptedStrategy(WINNING_SCHEDULE),
            ["BBB"],
            Interval.D1,
            FROM,
            TO,
            session=db_session,
            candle_store=store,
            initial_capital=Decimal("100000"),
            risk_per_trade_pct=Decimal("10"),
            cost_rates=CostConfig(),
            backtest_cfg=BacktestConfig(),
        )
        db_session.commit()
        run1 = db_session.get(BacktestRun, r1.backtest_run_id)
        run2 = db_session.get(BacktestRun, r2.backtest_run_id)
        assert run1 is not None
        assert run2 is not None
        assert run1.data_fingerprint == run2.data_fingerprint

    def test_profit_factor_infinity_round_trips_through_json_column(
        self, db_session: Session, store: CandleStore
    ) -> None:
        # A single winning round trip -> no losses -> profit_factor is +inf.
        _seed_instrument(db_session, "CCC", store, [100, 102, 104, 106, 108, 110, 112])
        result = run_backtest_for_symbols(
            ScriptedStrategy(WINNING_SCHEDULE),
            ["CCC"],
            Interval.D1,
            FROM,
            TO,
            session=db_session,
            candle_store=store,
            initial_capital=Decimal("100000"),
            risk_per_trade_pct=Decimal("10"),
            cost_rates=CostConfig(),
            backtest_cfg=BacktestConfig(),
        )
        assert result.portfolio_metrics.profit_factor == float("inf")
        db_session.commit()
        db_session.expire_all()

        reloaded = db_session.get(BacktestRun, result.backtest_run_id)
        assert reloaded is not None
        assert reloaded.metrics["portfolio"]["profit_factor"] == float("inf")


class TestMultiSymbolPortfolio:
    def test_capital_split_equally_and_metrics_present_per_symbol(
        self, db_session: Session, store: CandleStore
    ) -> None:
        _seed_instrument(db_session, "SYM1", store, [100, 102, 104, 106, 108, 110, 112])
        _seed_instrument(db_session, "SYM2", store, [200, 204, 208, 212, 216, 220, 224])

        result = run_backtest_for_symbols(
            ScriptedStrategy(WINNING_SCHEDULE),
            ["SYM1", "SYM2"],
            Interval.D1,
            FROM,
            TO,
            session=db_session,
            candle_store=store,
            initial_capital=Decimal("100000"),
            risk_per_trade_pct=Decimal("10"),
            cost_rates=CostConfig(),
            backtest_cfg=BacktestConfig(),
        )
        assert len(result.per_symbol) == 2
        assert {sr.symbol for sr in result.per_symbol} == {"SYM1", "SYM2"}
        # each symbol traded on its own 50,000 allocation, independently
        for sr in result.per_symbol:
            assert len(sr.result.trades) == 2
        # portfolio equity curve aggregates both -> starts at the full capital
        assert result.portfolio_metrics.total_trades == 4


class TestFreshStrategyInstancePerSymbol:
    """Orchestration-level guarantee (docs/architecture/ADRS.md ADR-016):

    run_backtest_for_symbols must construct a fresh strategy instance per
    symbol, not reuse the caller's one instance across the loop — defense in
    depth for any stateful strategy that (unlike ema_atr_stop.py) doesn't
    reset itself on every flat bar. LeakyOnceStrategy deliberately has no
    such reset logic, so this test fails if the orchestration regresses to
    instance reuse.
    """

    def test_every_symbol_gets_its_own_entry_signal(
        self, db_session: Session, store: CandleStore
    ) -> None:
        _seed_instrument(db_session, "FIRST", store, [100, 102, 104, 106, 108, 110, 112])
        _seed_instrument(db_session, "SECOND", store, [200, 202, 204, 206, 208, 210, 212])
        _seed_instrument(db_session, "THIRD", store, [300, 302, 304, 306, 308, 310, 312])

        shared_instance = LeakyOnceStrategy()
        result = run_backtest_for_symbols(
            shared_instance,
            ["FIRST", "SECOND", "THIRD"],
            Interval.D1,
            FROM,
            TO,
            session=db_session,
            candle_store=store,
            initial_capital=Decimal("300000"),
            risk_per_trade_pct=Decimal("10"),
            cost_rates=CostConfig(),
            backtest_cfg=BacktestConfig(),
        )
        # If the orchestration reused `shared_instance` across symbols, only
        # the first symbol's very first bar would see call_count==1 and
        # trade; SECOND and THIRD would silently get zero trades.
        for sr in result.per_symbol:
            assert len(sr.result.trades) == 1, f"{sr.symbol} got no entry — state leaked"
        # the caller's own instance must be untouched (proves a copy was used)
        assert shared_instance.call_count == 0
