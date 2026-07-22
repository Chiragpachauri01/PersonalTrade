"""Paper-trading soak tracking & go/no-go review (ROADMAP M18, ADR-028).

CLAUDE.md Rule 11 requires >=4 weeks of paper trading showing positive edge
net of realistic Indian costs before `trading.live_orders_enabled` is ever
flipped (ADR-008). This module answers the three questions a human running
that soak needs, each derived only from what M9-M12 already persist —
nothing here is a new source of truth:

1. "How far into the soak am I?" -- `compute_status()`.
2. "Is paper trading behaving like the backtest predicted?" --
   `compare_to_backtest()`, a weekly-review diff against the `BacktestRun`
   pinned as the soak's baseline at `pt soak start`. A real divergence here
   is a signal to investigate the simulator or the strategy, never to loosen
   the paper broker's fill/cost model to make the numbers agree (ROADMAP
   M18's own risk note).
3. "Is it time to flip the live switch?" -- `evaluate_go_no_go()`, which
   refuses a GO before `SoakConfig.target_days` regardless of how good
   interim numbers look, then checks the same metrics against
   `SoakConfig`'s statistical/risk thresholds.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from personaltrade.analytics.pnl import PnLSummary
from personaltrade.backtest.metrics import BacktestMetrics
from personaltrade.core.config import SoakConfig
from personaltrade.data.store.models import SoakPeriod

#: Field names shared by PnLSummary and BacktestMetrics (both trace back to
#: the same cagr/sharpe/max_drawdown/win_rate/expectancy/profit_factor
#: statistics — see analytics/pnl.py and backtest/metrics.py docstrings for
#: why they're independently computed rather than one shared dataclass).
_COMPARED_METRICS = ("cagr", "sharpe", "max_drawdown", "win_rate", "expectancy", "profit_factor")


@dataclass(frozen=True)
class SoakStatus:
    soak_id: int
    started_at: datetime
    target_days: int
    days_elapsed: int
    days_remaining: int
    baseline_backtest_run_id: int | None
    ended_at: datetime | None


def compute_status(soak: SoakPeriod, now: datetime) -> SoakStatus:
    """`now` is an explicit parameter (not `datetime.now()` internally) for
    the same reason ADR-018 made `RiskEngine.evaluate()`'s equity/P&L inputs
    explicit: a pure function of its inputs is trivially testable without
    freezing the clock."""
    reference = soak.ended_at or now
    days_elapsed = max(0, (reference - soak.started_at).days)
    return SoakStatus(
        soak_id=soak.id,
        started_at=soak.started_at,
        target_days=soak.target_days,
        days_elapsed=days_elapsed,
        days_remaining=max(0, soak.target_days - days_elapsed),
        baseline_backtest_run_id=soak.baseline_backtest_run_id,
        ended_at=soak.ended_at,
    )


@dataclass(frozen=True)
class MetricDelta:
    name: str
    paper: float
    backtest: float
    delta: float  # paper - backtest


@dataclass(frozen=True)
class SoakComparison:
    since: datetime
    paper: PnLSummary
    backtest: BacktestMetrics
    deltas: list[MetricDelta]


def compare_to_backtest(
    since: datetime, paper: PnLSummary, backtest: BacktestMetrics
) -> SoakComparison:
    deltas = [
        MetricDelta(
            name=name,
            paper=getattr(paper, name),
            backtest=getattr(backtest, name),
            delta=getattr(paper, name) - getattr(backtest, name),
        )
        for name in _COMPARED_METRICS
    ]
    return SoakComparison(since=since, paper=paper, backtest=backtest, deltas=deltas)


@dataclass(frozen=True)
class Criterion:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class GoNoGoResult:
    go: bool
    criteria: list[Criterion]


def evaluate_go_no_go(status: SoakStatus, paper: PnLSummary, cfg: SoakConfig) -> GoNoGoResult:
    """Every criterion is evaluated and reported even once one has already
    failed — a human reviewing this needs the full picture ("also only 12
    closed trades") in one pass, not a single fail-fast reason."""
    criteria = [
        Criterion(
            "min_soak_days",
            status.days_elapsed >= status.target_days,
            f"{status.days_elapsed}/{status.target_days} days elapsed",
        ),
        Criterion(
            "min_closed_trades",
            paper.closed_trades >= cfg.min_closed_trades,
            f"{paper.closed_trades} closed trades (need >= {cfg.min_closed_trades})",
        ),
        Criterion(
            "positive_net_pnl",
            (not cfg.require_positive_net_pnl) or paper.total_pnl > 0,
            f"total_pnl=₹{paper.total_pnl}",
        ),
        Criterion(
            "min_sharpe",
            paper.sharpe >= cfg.min_sharpe,
            f"sharpe={paper.sharpe:.2f} (need >= {cfg.min_sharpe})",
        ),
        Criterion(
            "max_drawdown",
            paper.max_drawdown <= cfg.max_drawdown_pct / 100.0,
            f"max_drawdown={paper.max_drawdown:.2%} (limit {cfg.max_drawdown_pct:.1f}%)",
        ),
    ]
    return GoNoGoResult(go=all(c.passed for c in criteria), criteria=criteria)
