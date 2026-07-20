"""Parameter sweep: the chronological (never shuffled) in-sample/out-of-sample
split is the whole point of this module (ROADMAP M7 overfitting guard) — the
split-boundary tests are the most important ones here.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from personaltrade.backtest.sweep import (
    InvalidSweepGrid,
    grid_combinations,
    run_parameter_sweep,
    split_date,
)
from personaltrade.core.config import BacktestConfig, CostConfig
from personaltrade.core.enums import Interval
from personaltrade.data.store.candles import CandleStore
from personaltrade.data.store.models import BacktestRun
from personaltrade.strategy.strategies.sma_crossover import SMACrossoverStrategy
from tests.test_backtest_run import _seed_instrument


class TestSplitDate:
    def test_hand_computed(self) -> None:
        # 10 days total, oos_fraction=0.3 -> in_sample_days=round(10*0.7)=7
        result = split_date(date(2026, 1, 1), date(2026, 1, 11), 0.3)
        assert result == date(2026, 1, 8)

    def test_larger_oos_fraction_moves_split_earlier(self) -> None:
        result = split_date(date(2026, 1, 1), date(2026, 1, 11), 0.5)
        assert result == date(2026, 1, 6)  # round(10*0.5)=5 days in-sample


class TestGridCombinations:
    def test_empty_grid_is_one_empty_combo(self) -> None:
        assert grid_combinations({}) == [{}]

    def test_single_param(self) -> None:
        assert grid_combinations({"fast_period": [5, 10]}) == [
            {"fast_period": 5},
            {"fast_period": 10},
        ]

    def test_cartesian_product_of_two_params(self) -> None:
        combos = grid_combinations({"fast_period": [5, 10], "slow_period": [20, 30]})
        assert len(combos) == 4
        assert {"fast_period": 5, "slow_period": 20} in combos
        assert {"fast_period": 10, "slow_period": 30} in combos

    def test_empty_value_list_yields_no_combos(self) -> None:
        assert grid_combinations({"fast_period": []}) == []


@pytest.fixture()
def store(tmp_path: Path) -> CandleStore:
    return CandleStore(tmp_path / "candles")


class TestRunParameterSweep:
    def test_rejects_oos_fraction_out_of_range(
        self, db_session: Session, store: CandleStore
    ) -> None:
        with pytest.raises(InvalidSweepGrid, match="oos_fraction"):
            run_parameter_sweep(
                SMACrossoverStrategy,
                {},
                ["X"],
                Interval.D1,
                date(2026, 1, 1),
                date(2026, 1, 10),
                session=db_session,
                candle_store=store,
                initial_capital=Decimal("100000"),
                risk_per_trade_pct=Decimal("10"),
                cost_rates=CostConfig(),
                backtest_cfg=BacktestConfig(),
                oos_fraction=1.5,
            )

    def test_rejects_empty_grid_combinations(self, db_session: Session, store: CandleStore) -> None:
        with pytest.raises(InvalidSweepGrid, match="no combinations"):
            run_parameter_sweep(
                SMACrossoverStrategy,
                {"fast_period": []},
                ["X"],
                Interval.D1,
                date(2026, 1, 1),
                date(2026, 1, 10),
                session=db_session,
                candle_store=store,
                initial_capital=Decimal("100000"),
                risk_per_trade_pct=Decimal("10"),
                cost_rates=CostConfig(),
                backtest_cfg=BacktestConfig(),
            )

    def test_split_leaving_no_room_rejected(self, db_session: Session, store: CandleStore) -> None:
        with pytest.raises(InvalidSweepGrid, match="leaves no room"):
            run_parameter_sweep(
                SMACrossoverStrategy,
                {},
                ["X"],
                Interval.D1,
                date(2026, 1, 1),
                date(2026, 1, 2),  # 1 day total: no split point strictly between
                session=db_session,
                candle_store=store,
                initial_capital=Decimal("100000"),
                risk_per_trade_pct=Decimal("10"),
                cost_rates=CostConfig(),
                backtest_cfg=BacktestConfig(),
                oos_fraction=0.5,
            )

    def test_invalid_combo_recorded_as_error_not_raised(
        self, db_session: Session, store: CandleStore
    ) -> None:
        _seed_instrument(
            db_session,
            "ZZZ",
            store,
            list(range(100, 160)),  # 60 days of data
        )
        results = run_parameter_sweep(
            SMACrossoverStrategy,
            {"fast_period": [10, 30], "slow_period": [20]},  # fast=30 >= slow=20 is invalid
            ["ZZZ"],
            Interval.D1,
            date(2026, 1, 1),
            date(2026, 3, 1),
            session=db_session,
            candle_store=store,
            initial_capital=Decimal("100000"),
            risk_per_trade_pct=Decimal("10"),
            cost_rates=CostConfig(),
            backtest_cfg=BacktestConfig(),
        )
        assert len(results) == 2
        by_fast = {r.params["fast_period"]: r for r in results}
        assert by_fast[10].error is None
        assert by_fast[10].in_sample is not None
        assert by_fast[10].out_of_sample is not None
        assert by_fast[30].error is not None
        assert by_fast[30].in_sample is None
        assert by_fast[30].out_of_sample is None

    def test_windows_are_chronological_not_shuffled(
        self, db_session: Session, store: CandleStore
    ) -> None:
        """Verifies the orchestration by checking the persisted BacktestRun
        date boundaries directly — sidesteps needing to predict strategy
        behavior, and is a precise, deterministic check of ADR-017."""
        _seed_instrument(db_session, "YYY", store, list(range(100, 160)))
        from_date, to_date = date(2026, 1, 1), date(2026, 3, 1)  # 59 days
        expected_split = split_date(from_date, to_date, 0.4)

        run_parameter_sweep(
            SMACrossoverStrategy,
            {"fast_period": [5]},
            ["YYY"],
            Interval.D1,
            from_date,
            to_date,
            session=db_session,
            candle_store=store,
            initial_capital=Decimal("100000"),
            risk_per_trade_pct=Decimal("10"),
            cost_rates=CostConfig(),
            backtest_cfg=BacktestConfig(),
            oos_fraction=0.4,
        )
        db_session.commit()

        runs = db_session.query(BacktestRun).order_by(BacktestRun.id).all()
        assert len(runs) == 2
        in_sample_run, out_sample_run = runs
        assert in_sample_run.from_date == from_date
        assert in_sample_run.to_date == expected_split
        assert out_sample_run.from_date == expected_split
        assert out_sample_run.to_date == to_date
        # strictly sequential in time, no overlap, no shuffling
        assert in_sample_run.to_date == out_sample_run.from_date
        assert in_sample_run.from_date < in_sample_run.to_date < out_sample_run.to_date
