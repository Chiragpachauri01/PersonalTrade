"""analytics/soak.py (ROADMAP M18, ADR-028): soak status, paper-vs-backtest
comparison, and the go/no-go review gate."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from personaltrade.analytics.pnl import PnLSummary
from personaltrade.analytics.soak import compare_to_backtest, compute_status, evaluate_go_no_go
from personaltrade.backtest.metrics import BacktestMetrics
from personaltrade.core.config import SoakConfig
from personaltrade.data.store.models import SoakPeriod

_CFG = SoakConfig(
    target_days=28, min_closed_trades=20, min_sharpe=0.0, max_drawdown_pct=25.0,
    require_positive_net_pnl=True,
)


def _summary(**overrides: object) -> PnLSummary:
    defaults: dict[str, object] = dict(
        realized_pnl=Decimal("1000"),
        unrealized_pnl=Decimal("0"),
        total_pnl=Decimal("1000"),
        win_rate=0.6,
        expectancy=50.0,
        profit_factor=1.8,
        closed_trades=25,
        cagr=0.12,
        sharpe=1.1,
        max_drawdown=0.10,
    )
    defaults.update(overrides)
    return PnLSummary(**defaults)  # type: ignore[arg-type]


def _backtest_metrics(**overrides: object) -> BacktestMetrics:
    defaults: dict[str, object] = dict(
        cagr=0.15, sharpe=1.3, max_drawdown=0.08, win_rate=0.55, expectancy=45.0,
        profit_factor=1.6, total_trades=40, closed_trades=20,
    )
    defaults.update(overrides)
    return BacktestMetrics(**defaults)  # type: ignore[arg-type]


class TestComputeStatus:
    def test_elapsed_and_remaining_days(self) -> None:
        soak = SoakPeriod(
            id=1, started_at=datetime(2026, 1, 1, tzinfo=UTC), target_days=28,
            baseline_backtest_run_id=7,
        )
        status = compute_status(soak, datetime(2026, 1, 15, tzinfo=UTC))
        assert status.days_elapsed == 14
        assert status.days_remaining == 14
        assert status.baseline_backtest_run_id == 7

    def test_days_remaining_never_negative_past_target(self) -> None:
        soak = SoakPeriod(id=1, started_at=datetime(2026, 1, 1, tzinfo=UTC), target_days=28)
        status = compute_status(soak, datetime(2026, 3, 1, tzinfo=UTC))
        assert status.days_elapsed == 59
        assert status.days_remaining == 0

    def test_ended_soak_freezes_elapsed_at_end_time(self) -> None:
        soak = SoakPeriod(
            id=1,
            started_at=datetime(2026, 1, 1, tzinfo=UTC),
            target_days=28,
            ended_at=datetime(2026, 1, 10, tzinfo=UTC),
        )
        # `now` is much later, but the soak already ended -> elapsed is frozen at ended_at
        status = compute_status(soak, datetime(2026, 6, 1, tzinfo=UTC))
        assert status.days_elapsed == 9


class TestCompareToBacktest:
    def test_deltas_are_paper_minus_backtest(self) -> None:
        paper = _summary(sharpe=1.1, cagr=0.12)
        backtest = _backtest_metrics(sharpe=1.3, cagr=0.15)
        comparison = compare_to_backtest(datetime(2026, 1, 1, tzinfo=UTC), paper, backtest)

        by_name = {d.name: d for d in comparison.deltas}
        assert by_name["sharpe"].paper == 1.1
        assert by_name["sharpe"].backtest == 1.3
        assert by_name["sharpe"].delta == 1.1 - 1.3
        assert by_name["cagr"].delta == 0.12 - 0.15
        assert {d.name for d in comparison.deltas} == {
            "cagr", "sharpe", "max_drawdown", "win_rate", "expectancy", "profit_factor",
        }


class TestEvaluateGoNoGo:
    def test_all_criteria_pass_is_a_go(self) -> None:
        status = compute_status(
            SoakPeriod(id=1, started_at=datetime(2026, 1, 1, tzinfo=UTC), target_days=28),
            datetime(2026, 1, 29, tzinfo=UTC),
        )
        result = evaluate_go_no_go(status, _summary(), _CFG)
        assert result.go is True
        assert all(c.passed for c in result.criteria)

    def test_too_few_days_is_a_no_go_even_with_great_numbers(self) -> None:
        status = compute_status(
            SoakPeriod(id=1, started_at=datetime(2026, 1, 1, tzinfo=UTC), target_days=28),
            datetime(2026, 1, 15, tzinfo=UTC),  # only 14 days
        )
        result = evaluate_go_no_go(status, _summary(sharpe=5.0, total_pnl=Decimal("100000")), _CFG)
        assert result.go is False
        by_name = {c.name: c for c in result.criteria}
        assert by_name["min_soak_days"].passed is False

    def test_too_few_closed_trades_is_a_no_go(self) -> None:
        status = compute_status(
            SoakPeriod(id=1, started_at=datetime(2026, 1, 1, tzinfo=UTC), target_days=28),
            datetime(2026, 1, 29, tzinfo=UTC),
        )
        result = evaluate_go_no_go(status, _summary(closed_trades=3), _CFG)
        assert result.go is False
        by_name = {c.name: c for c in result.criteria}
        assert by_name["min_closed_trades"].passed is False

    def test_negative_net_pnl_is_a_no_go(self) -> None:
        status = compute_status(
            SoakPeriod(id=1, started_at=datetime(2026, 1, 1, tzinfo=UTC), target_days=28),
            datetime(2026, 1, 29, tzinfo=UTC),
        )
        result = evaluate_go_no_go(status, _summary(total_pnl=Decimal("-500")), _CFG)
        assert result.go is False
        by_name = {c.name: c for c in result.criteria}
        assert by_name["positive_net_pnl"].passed is False

    def test_drawdown_beyond_limit_is_a_no_go(self) -> None:
        status = compute_status(
            SoakPeriod(id=1, started_at=datetime(2026, 1, 1, tzinfo=UTC), target_days=28),
            datetime(2026, 1, 29, tzinfo=UTC),
        )
        result = evaluate_go_no_go(status, _summary(max_drawdown=0.40), _CFG)
        assert result.go is False
        by_name = {c.name: c for c in result.criteria}
        assert by_name["max_drawdown"].passed is False

    def test_sharpe_below_minimum_is_a_no_go(self) -> None:
        status = compute_status(
            SoakPeriod(id=1, started_at=datetime(2026, 1, 1, tzinfo=UTC), target_days=28),
            datetime(2026, 1, 29, tzinfo=UTC),
        )
        result = evaluate_go_no_go(status, _summary(sharpe=-0.5), _CFG)
        assert result.go is False
        by_name = {c.name: c for c in result.criteria}
        assert by_name["min_sharpe"].passed is False

    def test_require_positive_net_pnl_false_skips_that_gate(self) -> None:
        cfg = SoakConfig(
            target_days=28, min_closed_trades=20, min_sharpe=0.0, max_drawdown_pct=25.0,
            require_positive_net_pnl=False,
        )
        status = compute_status(
            SoakPeriod(id=1, started_at=datetime(2026, 1, 1, tzinfo=UTC), target_days=28),
            datetime(2026, 1, 29, tzinfo=UTC),
        )
        result = evaluate_go_no_go(status, _summary(total_pnl=Decimal("-500")), cfg)
        by_name = {c.name: c for c in result.criteria}
        assert by_name["positive_net_pnl"].passed is True
        assert result.go is True
