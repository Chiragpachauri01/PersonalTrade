"""Evaluates one Strategy against one instrument's most recent candle,
reusing the exact indicator-precompute + warmup-gating rules the backtest
engine uses (backtest/indicator_bridge.py, ADR-015 point 1) so "today's
signal" here can never disagree with what the same candle would have
produced mid-backtest. Read-only: no order flow, no persisted Signal/
StrategyRun rows (those belong to the live orchestrator, ADR-022) — the
Recommendation Engine is a standalone daily screener, not a trading loop.
"""

from __future__ import annotations

import pandas as pd

from personaltrade.backtest.indicator_bridge import (
    BatchIndicatorView,
    compute_indicator_set,
    first_all_valid_index,
)
from personaltrade.strategy.base import PositionView, Signal, Strategy, StrategyContext


def latest_signal(
    strategy: Strategy, candles: pd.DataFrame, position: PositionView
) -> Signal | None:
    """The Signal `strategy` would emit on `candles`' final bar, or None if
    the strategy is still warming up, declines to act, or `candles` is empty.
    """
    if candles.empty:
        return None

    candles = candles.reset_index(drop=True)
    index = len(candles) - 1
    indicator_series = compute_indicator_set(candles, strategy.required_indicators())
    effective_start = max(strategy.warmup_bars(), first_all_valid_index(indicator_series))
    if index < effective_start:
        return None

    ctx = StrategyContext(
        index=index,
        ts=candles["ts"].iloc[index].to_pydatetime(),
        candles=candles.iloc[: index + 1],
        indicators=BatchIndicatorView(indicator_series, index),
        position=position,
    )
    return strategy.on_candle(ctx)
