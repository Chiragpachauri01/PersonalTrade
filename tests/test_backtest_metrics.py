"""Metrics: hand-computed goldens plus cross-checks against Python's stdlib
`statistics` module (an independent implementation of mean/stdev, in the
spirit of M5's double-entry reference tests).
"""

from __future__ import annotations

import statistics
from datetime import UTC, datetime
from decimal import Decimal
from typing import ClassVar

import pytest

from personaltrade.backtest.costs import TradeCosts
from personaltrade.backtest.engine import ExecutedTrade
from personaltrade.backtest.metrics import (
    cagr,
    compute_metrics_from_series,
    expectancy,
    max_drawdown,
    period_returns,
    profit_factor,
    sharpe_ratio,
    win_rate,
)
from personaltrade.core.enums import Side

ZERO_COSTS = TradeCosts(
    brokerage=Decimal("0"),
    stt=Decimal("0"),
    stamp_duty=Decimal("0"),
    exchange_charges=Decimal("0"),
    sebi_charges=Decimal("0"),
    gst=Decimal("0"),
    total=Decimal("0"),
    net_amount=Decimal("0"),
)


def _trade(realized_pnl: float | None) -> ExecutedTrade:
    return ExecutedTrade(
        index=0,
        ts=datetime(2026, 1, 1, tzinfo=UTC),
        side=Side.SELL,
        qty=1,
        price=Decimal("100"),
        costs=ZERO_COSTS,
        signal_index=0,
        realized_pnl=None if realized_pnl is None else Decimal(str(realized_pnl)),
    )


def _series(pairs: list[tuple[str, float]]) -> list[tuple[datetime, float]]:
    return [(datetime.fromisoformat(d).replace(tzinfo=UTC), v) for d, v in pairs]


class TestCAGR:
    def test_flat_equity_is_zero(self) -> None:
        series = _series([("2026-01-01", 100000.0), ("2026-06-01", 100000.0)])
        assert cagr(series) == 0.0

    def test_matches_independent_formula_for_a_doubling(self) -> None:
        series = _series([("2024-01-01", 100000.0), ("2026-01-01", 400000.0)])
        days = (series[-1][0] - series[0][0]).days
        expected = 4.0 ** (1.0 / (days / 365.25)) - 1.0
        assert cagr(series) == pytest.approx(expected)

    def test_fewer_than_two_points_is_zero(self) -> None:
        assert cagr([]) == 0.0
        assert cagr(_series([("2026-01-01", 100.0)])) == 0.0

    def test_total_loss_returns_minus_one(self) -> None:
        series = _series([("2026-01-01", 100000.0), ("2026-06-01", 0.0)])
        assert cagr(series) == -1.0

    def test_same_day_is_zero(self) -> None:
        ts = datetime(2026, 1, 1, tzinfo=UTC)
        assert cagr([(ts, 100.0), (ts, 200.0)]) == 0.0


class TestPeriodReturns:
    def test_hand_computed(self) -> None:
        series = _series(
            [
                ("2026-01-01", 100.0),
                ("2026-01-02", 102.0),
                ("2026-01-03", 101.0),
                ("2026-01-04", 104.0),
            ]
        )
        returns = period_returns(series)
        assert returns == pytest.approx([0.02, -0.00980392, 0.02970297], abs=1e-6)


class TestSharpeRatio:
    def test_matches_stdlib_statistics_module(self) -> None:
        series = _series(
            [
                ("2026-01-01", 100.0),
                ("2026-01-02", 102.0),
                ("2026-01-03", 101.0),
                ("2026-01-04", 104.0),
            ]
        )
        returns = period_returns(series)
        expected_mean = statistics.mean(returns)
        expected_std = statistics.stdev(returns)  # sample stdev, ddof=1 — matches sharpe_ratio
        expected = (expected_mean / expected_std) * (252**0.5)
        assert sharpe_ratio(series) == pytest.approx(expected)

    def test_fewer_than_two_returns_is_zero(self) -> None:
        assert sharpe_ratio(_series([("2026-01-01", 100.0)])) == 0.0

    def test_zero_variance_is_zero(self) -> None:
        # exactly +1% each day (geometric, not linear) -> identical returns ->
        # zero variance -> guarded to 0.0, not a division by zero
        series = _series(
            [
                ("2026-01-01", 100.0),
                ("2026-01-02", 101.0),
                ("2026-01-03", 102.01),
                ("2026-01-04", 103.0301),
            ]
        )
        assert period_returns(series) == pytest.approx([0.01, 0.01, 0.01])
        assert sharpe_ratio(series) == 0.0


class TestMaxDrawdown:
    def test_hand_computed(self) -> None:
        # peaks: 100,120,120,120,120,130; troughs relative to running peak:
        # 0, 0, (120-90)/120=0.25, (120-110)/120=0.0833, (120-80)/120=0.3333, 0
        series = _series(
            [
                ("2026-01-01", 100.0),
                ("2026-01-02", 120.0),
                ("2026-01-03", 90.0),
                ("2026-01-04", 110.0),
                ("2026-01-05", 80.0),
                ("2026-01-06", 130.0),
            ]
        )
        assert max_drawdown(series) == pytest.approx(1 / 3)

    def test_monotonic_increase_has_zero_drawdown(self) -> None:
        series = _series([("2026-01-01", 100.0), ("2026-01-02", 110.0), ("2026-01-03", 120.0)])
        assert max_drawdown(series) == 0.0

    def test_empty_series_is_zero(self) -> None:
        assert max_drawdown([]) == 0.0


class TestTradeMetrics:
    """trades: [+100, -50, +200, -100, +300] -> win_rate=0.6, expectancy=90, profit_factor=4.0."""

    TRADES: ClassVar = [_trade(100), _trade(-50), _trade(200), _trade(-100), _trade(300)]

    def test_win_rate(self) -> None:
        assert win_rate(self.TRADES) == pytest.approx(0.6)

    def test_expectancy(self) -> None:
        assert expectancy(self.TRADES) == pytest.approx(90.0)

    def test_profit_factor(self) -> None:
        assert profit_factor(self.TRADES) == pytest.approx(4.0)

    def test_open_trades_excluded_from_metrics(self) -> None:
        # an unclosed leg (realized_pnl=None) must not count toward win_rate's denominator
        trades = [*self.TRADES, _trade(None)]
        assert win_rate(trades) == pytest.approx(0.6)

    def test_no_closed_trades_is_zero(self) -> None:
        assert win_rate([]) == 0.0
        assert expectancy([]) == 0.0
        assert profit_factor([]) == 0.0

    def test_profit_factor_no_losses_is_inf(self) -> None:
        assert profit_factor([_trade(100), _trade(50)]) == float("inf")

    def test_profit_factor_no_wins_is_zero(self) -> None:
        assert profit_factor([_trade(-100), _trade(-50)]) == 0.0


class TestComputeMetricsAggregator:
    def test_bundles_all_submetrics(self) -> None:
        series = _series(
            [("2026-01-01", 100000.0), ("2026-01-02", 101000.0), ("2026-01-03", 100500.0)]
        )
        trades = [_trade(500), _trade(-200)]
        result = compute_metrics_from_series(series, trades)
        assert result.cagr == cagr(series)
        assert result.sharpe == sharpe_ratio(series)
        assert result.max_drawdown == max_drawdown(series)
        assert result.win_rate == win_rate(trades)
        assert result.expectancy == expectancy(trades)
        assert result.profit_factor == profit_factor(trades)
        assert result.total_trades == 2
        assert result.closed_trades == 2
