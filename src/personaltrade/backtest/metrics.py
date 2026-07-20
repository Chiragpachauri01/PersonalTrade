"""Backtest performance metrics — pure functions over an equity curve and trade list.

These are analytical statistics (CAGR, Sharpe, drawdown, ...), not ledger
money: computed in float, per ADR-011's principle that statistics are
float64 while transactional money (which they're derived from) is Decimal.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from personaltrade.backtest.engine import EquityPoint, ExecutedTrade

TRADING_DAYS_PER_YEAR = 252

#: (timestamp, equity) pairs, ascending. Deliberately not tied to EquityPoint's
#: Decimal per-symbol ledger fields — a portfolio-level curve aggregated across
#: multiple symbols (backtest/run.py) has no single meaningful cash/qty, only
#: a summed equity value, so metrics operate on this lighter shape instead.
EquitySeries = Sequence[tuple[datetime, float]]


def equity_series_from_curve(equity_curve: list[EquityPoint]) -> EquitySeries:
    return [(p.ts, float(p.equity)) for p in equity_curve]


def cagr(series: EquitySeries) -> float:
    if len(series) < 2:
        return 0.0
    start_ts, start = series[0]
    end_ts, end = series[-1]
    if start <= 0:
        return 0.0
    days = (end_ts - start_ts).days
    if days <= 0:
        return 0.0
    years = days / 365.25
    ratio = end / start
    if ratio <= 0:
        return -1.0
    result: float = ratio ** (1.0 / years) - 1.0
    return result


def period_returns(series: EquitySeries) -> list[float]:
    values = [v for _, v in series]
    return [
        (values[i] / values[i - 1] - 1.0) if values[i - 1] != 0 else 0.0
        for i in range(1, len(values))
    ]


def sharpe_ratio(series: EquitySeries, periods_per_year: int = TRADING_DAYS_PER_YEAR) -> float:
    """Annualized Sharpe of the series' period returns (sample std, ddof=1)."""
    returns = period_returns(series)
    if len(returns) < 2:
        return 0.0
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    std = variance**0.5
    if std == 0:
        return 0.0
    result: float = (mean / std) * (periods_per_year**0.5)
    return result


def max_drawdown(series: EquitySeries) -> float:
    """Maximum peak-to-trough decline, as a positive fraction (0.23 = 23% drawdown)."""
    if not series:
        return 0.0
    peak = series[0][1]
    worst = 0.0
    for _, value in series:
        peak = max(peak, value)
        if peak > 0:
            worst = max(worst, (peak - value) / peak)
    return worst


def _closed_trade_pnls(trades: list[ExecutedTrade]) -> list[float]:
    return [float(t.realized_pnl) for t in trades if t.realized_pnl is not None]


def win_rate(trades: list[ExecutedTrade]) -> float:
    pnls = _closed_trade_pnls(trades)
    if not pnls:
        return 0.0
    return sum(1 for p in pnls if p > 0) / len(pnls)


def expectancy(trades: list[ExecutedTrade]) -> float:
    """Average realized P&L per closed round-trip trade, in rupees."""
    pnls = _closed_trade_pnls(trades)
    return sum(pnls) / len(pnls) if pnls else 0.0


def profit_factor(trades: list[ExecutedTrade]) -> float:
    """Gross wins / gross losses. inf if there are wins and no losses; 0 if no wins."""
    pnls = _closed_trade_pnls(trades)
    gross_win = sum(p for p in pnls if p > 0)
    gross_loss = -sum(p for p in pnls if p < 0)
    if gross_loss == 0:
        return float("inf") if gross_win > 0 else 0.0
    return gross_win / gross_loss


@dataclass(frozen=True)
class BacktestMetrics:
    cagr: float
    sharpe: float
    max_drawdown: float
    win_rate: float
    expectancy: float
    profit_factor: float
    total_trades: int  # every executed leg (opens + closes)
    closed_trades: int  # round trips only — the denominator for win_rate/expectancy


def compute_metrics(
    equity_curve: list[EquityPoint], trades: list[ExecutedTrade]
) -> BacktestMetrics:
    series = equity_series_from_curve(equity_curve)
    return BacktestMetrics(
        cagr=cagr(series),
        sharpe=sharpe_ratio(series),
        max_drawdown=max_drawdown(series),
        win_rate=win_rate(trades),
        expectancy=expectancy(trades),
        profit_factor=profit_factor(trades),
        total_trades=len(trades),
        closed_trades=len(_closed_trade_pnls(trades)),
    )


def compute_metrics_from_series(
    series: EquitySeries, trades: list[ExecutedTrade]
) -> BacktestMetrics:
    """For a portfolio-level (multi-symbol aggregated) equity series — see backtest/run.py."""
    return BacktestMetrics(
        cagr=cagr(series),
        sharpe=sharpe_ratio(series),
        max_drawdown=max_drawdown(series),
        win_rate=win_rate(trades),
        expectancy=expectancy(trades),
        profit_factor=profit_factor(trades),
        total_trades=len(trades),
        closed_trades=len(_closed_trade_pnls(trades)),
    )
