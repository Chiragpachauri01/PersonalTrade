"""Parameter-sweep orchestration with an enforced chronological in-sample /
out-of-sample split (ROADMAP M7 — "overfitting during sweeps: out-of-sample
split enforced in tooling", not left as a discipline the user must remember).

The split point is a plain date split — never a shuffle — so out-of-sample
data is always strictly later in time than in-sample data, the same
no-look-ahead discipline the backtest engine itself enforces (ADR-015). Each
window runs as its own independent backtest (fresh capital, fresh strategy
instance) via the existing M6 persistence path in backtest/run.py — a sweep
is orchestration on top of what M6 already built, not a new execution engine.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from personaltrade.backtest.metrics import BacktestMetrics
from personaltrade.backtest.run import run_backtest_for_symbols
from personaltrade.core.config import BacktestConfig, CostConfig
from personaltrade.core.enums import Interval
from personaltrade.core.errors import PersonalTradeError
from personaltrade.data.store.candles import CandleStore
from personaltrade.strategy.base import Strategy, construct_strategy


class InvalidSweepGrid(PersonalTradeError):
    """The parameter grid or oos_fraction is unusable."""


@dataclass(frozen=True)
class SweepResult:
    params: dict[str, Any]
    in_sample: BacktestMetrics | None
    out_of_sample: BacktestMetrics | None
    error: str | None


def split_date(from_date: date, to_date: date, oos_fraction: float) -> date:
    """The date the out-of-sample window begins — a chronological cut, never a shuffle."""
    total_days = (to_date - from_date).days
    in_sample_days = round(total_days * (1 - oos_fraction))
    return from_date + timedelta(days=in_sample_days)


def grid_combinations(grid: dict[str, list[Any]]) -> list[dict[str, Any]]:
    """Cartesian product of a {param_name: [values]} grid.

    Empty grid -> one empty combo (defaults).
    """
    if not grid:
        return [{}]
    keys = list(grid)
    return [dict(zip(keys, values, strict=True)) for values in itertools.product(*grid.values())]


def run_parameter_sweep(
    strategy_cls: type[Strategy],
    grid: dict[str, list[Any]],
    symbols: list[str],
    interval: Interval,
    from_date: date,
    to_date: date,
    *,
    session: Session,
    candle_store: CandleStore,
    initial_capital: Decimal,
    risk_per_trade_pct: Decimal,
    cost_rates: CostConfig,
    backtest_cfg: BacktestConfig,
    oos_fraction: float = 0.3,
    exchange: str = "NSE",
) -> list[SweepResult]:
    """Run every combination in `grid` on both an in-sample and an out-of-sample
    window. A combo that fails validation or has no data for a window is
    recorded with `.error` set, not raised — one bad combo shouldn't abort
    the rest of the sweep.
    """
    if not (0.0 < oos_fraction < 1.0):
        raise InvalidSweepGrid(f"oos_fraction must be in (0, 1), got {oos_fraction}")
    combos = grid_combinations(grid)
    if not combos:
        raise InvalidSweepGrid("parameter grid produced no combinations")

    split = split_date(from_date, to_date, oos_fraction)
    if not (from_date < split < to_date):
        raise InvalidSweepGrid(
            f"oos_fraction={oos_fraction} leaves no room for both windows "
            f"in range [{from_date}, {to_date}]"
        )

    results: list[SweepResult] = []
    for combo in combos:
        try:
            params = strategy_cls.params_schema.model_validate(combo)
        except ValueError as exc:
            results.append(SweepResult(combo, None, None, str(exc)))
            continue

        in_sample_metrics: BacktestMetrics | None = None
        out_of_sample_metrics: BacktestMetrics | None = None
        error: str | None = None
        try:
            in_sample_run = run_backtest_for_symbols(
                construct_strategy(strategy_cls, params),
                symbols,
                interval,
                from_date,
                split,
                session=session,
                candle_store=candle_store,
                initial_capital=initial_capital,
                risk_per_trade_pct=risk_per_trade_pct,
                cost_rates=cost_rates,
                backtest_cfg=backtest_cfg,
                exchange=exchange,
            )
            in_sample_metrics = in_sample_run.portfolio_metrics

            out_sample_run = run_backtest_for_symbols(
                construct_strategy(strategy_cls, params),
                symbols,
                interval,
                split,
                to_date,
                session=session,
                candle_store=candle_store,
                initial_capital=initial_capital,
                risk_per_trade_pct=risk_per_trade_pct,
                cost_rates=cost_rates,
                backtest_cfg=backtest_cfg,
                exchange=exchange,
            )
            out_of_sample_metrics = out_sample_run.portfolio_metrics
        except (PersonalTradeError, ValueError) as exc:
            error = str(exc)

        results.append(SweepResult(combo, in_sample_metrics, out_of_sample_metrics, error))

    return results
