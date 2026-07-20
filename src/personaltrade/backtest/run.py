"""Orchestration: load candles -> run the engine per symbol -> aggregate -> persist.

Mirrors data/historical/sync.py's shape. Multi-symbol runs split initial
capital equally across symbols and simulate each independently — no
cross-symbol position limits or correlation (that's Risk Engine territory,
M8); portfolio-level equity is each symbol's equity curve summed by
timestamp (forward/back-filled across any per-symbol data gaps).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pandas as pd
from sqlalchemy.orm import Session

from personaltrade.backtest.engine import BacktestResult, ExecutedTrade, run_backtest
from personaltrade.backtest.metrics import (
    BacktestMetrics,
    EquitySeries,
    compute_metrics,
    compute_metrics_from_series,
)
from personaltrade.core.config import BacktestConfig, CostConfig
from personaltrade.core.enums import Interval, Segment
from personaltrade.core.errors import PersonalTradeError
from personaltrade.core.logging import get_logger
from personaltrade.data.store.candles import CandleStore
from personaltrade.data.store.models import BacktestRun, BacktestTrade
from personaltrade.data.store.repos import BacktestRunRepository, InstrumentRepository
from personaltrade.risk.sizing import FixedFractionalSizer
from personaltrade.strategy.base import Strategy

logger = get_logger(__name__)


class NoDataForSymbol(PersonalTradeError):
    """A requested symbol has no stored candles, or isn't in the instruments table."""


@dataclass(frozen=True)
class SymbolResult:
    symbol: str
    instrument_id: int
    result: BacktestResult
    metrics: BacktestMetrics


@dataclass(frozen=True)
class MultiSymbolResult:
    backtest_run_id: int
    per_symbol: list[SymbolResult]
    portfolio_metrics: BacktestMetrics


def _fingerprint(
    strategy_name: str,
    params_json: str,
    symbol_frames: dict[str, pd.DataFrame],
    from_date: date,
    to_date: date,
) -> str:
    """Reproducibility fingerprint: strategy+params+range+actual candle content.

    Hashing candle content (not just row counts) means a later source-data
    revision changes the fingerprint, flagging that an old run is no longer
    reproducible from current data — the whole point of this field.
    """
    hasher = hashlib.sha256()
    hasher.update(strategy_name.encode())
    hasher.update(params_json.encode())
    hasher.update(from_date.isoformat().encode())
    hasher.update(to_date.isoformat().encode())
    for symbol in sorted(symbol_frames):
        hasher.update(symbol.encode())
        row_hashes = pd.util.hash_pandas_object(symbol_frames[symbol], index=False)
        hasher.update(row_hashes.to_numpy().tobytes())
    return hasher.hexdigest()


def _aggregate_equity(per_symbol: list[SymbolResult]) -> EquitySeries:
    frames = []
    for sr in per_symbol:
        if not sr.result.equity_curve:
            continue
        frames.append(pd.Series({p.ts: float(p.equity) for p in sr.result.equity_curve}))
    if not frames:
        return []
    combined = pd.concat(frames, axis=1).sort_index().ffill().bfill()
    totals = combined.sum(axis=1)
    timestamps = [pd.Timestamp(ts).to_pydatetime() for ts in totals.index]
    return list(zip(timestamps, totals.to_numpy().tolist(), strict=True))


def _metrics_dict(metrics: BacktestMetrics) -> dict[str, Any]:
    return {
        "cagr": metrics.cagr,
        "sharpe": metrics.sharpe,
        "max_drawdown": metrics.max_drawdown,
        "win_rate": metrics.win_rate,
        "expectancy": metrics.expectancy,
        "profit_factor": metrics.profit_factor,
        "total_trades": metrics.total_trades,
        "closed_trades": metrics.closed_trades,
    }


def _trade_detail(trade: ExecutedTrade, symbol: str) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "index": trade.index,
        "ts": trade.ts.isoformat(),
        "side": str(trade.side),
        "qty": trade.qty,
        "price": str(trade.price),
        "signal_index": trade.signal_index,
        "realized_pnl": str(trade.realized_pnl) if trade.realized_pnl is not None else None,
        "costs": {
            "brokerage": str(trade.costs.brokerage),
            "stt": str(trade.costs.stt),
            "stamp_duty": str(trade.costs.stamp_duty),
            "exchange_charges": str(trade.costs.exchange_charges),
            "sebi_charges": str(trade.costs.sebi_charges),
            "gst": str(trade.costs.gst),
            "total": str(trade.costs.total),
            "net_amount": str(trade.costs.net_amount),
        },
    }


def run_backtest_for_symbols(
    strategy: Strategy,
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
    exchange: str = "NSE",
) -> MultiSymbolResult:
    if not symbols:
        raise NoDataForSymbol("no symbols given")

    instrument_repo = InstrumentRepository(session)
    per_symbol_capital = initial_capital / len(symbols)
    segment = Segment(backtest_cfg.default_segment)
    sizer = FixedFractionalSizer(risk_per_trade_pct)
    range_start = datetime.combine(from_date, datetime.min.time(), tzinfo=UTC)
    range_end = datetime.combine(to_date, datetime.max.time(), tzinfo=UTC)

    per_symbol: list[SymbolResult] = []
    symbol_frames: dict[str, pd.DataFrame] = {}
    for symbol in symbols:
        instrument = instrument_repo.get_by_symbol(symbol, exchange)
        if instrument is None:
            raise NoDataForSymbol(f"{symbol} ({exchange}) not in instruments table")

        candles = candle_store.read(
            symbol, exchange, interval, from_ts=range_start, to_ts=range_end
        )
        if candles.empty:
            raise NoDataForSymbol(
                f"no stored {interval.value} candles for {symbol} in [{from_date}, {to_date}]"
            )
        symbol_frames[symbol] = candles

        # A fresh instance per symbol, not the shared `strategy` object: a
        # stateful strategy (e.g. ema_atr_stop's in-position stop level)
        # must never carry state from one symbol's run into the next's.
        # clone() is safe because it reuses `strategy.params`, which already
        # passed validation once. Defense in depth on top of each strategy's
        # own obligation to reset state whenever flat.
        symbol_strategy = strategy.clone()
        result = run_backtest(
            symbol_strategy,
            candles,
            initial_capital=per_symbol_capital,
            sizer=sizer,
            cost_rates=cost_rates,
            segment=segment,
            slippage_bps=backtest_cfg.slippage_bps,
        )
        metrics = compute_metrics(result.equity_curve, result.trades)
        per_symbol.append(
            SymbolResult(symbol=symbol, instrument_id=instrument.id, result=result, metrics=metrics)
        )
        logger.info(
            "backtest_symbol_complete",
            symbol=symbol,
            trades=len(result.trades),
            cagr=metrics.cagr,
            max_drawdown=metrics.max_drawdown,
        )

    all_trades = [t for sr in per_symbol for t in sr.result.trades]
    portfolio_metrics = compute_metrics_from_series(_aggregate_equity(per_symbol), all_trades)
    fingerprint = _fingerprint(
        strategy.name, strategy.params.model_dump_json(), symbol_frames, from_date, to_date
    )

    run_row = BacktestRun(
        strategy_name=strategy.name,
        params=strategy.params.model_dump(mode="json"),
        cost_model_version=cost_rates.model_dump(mode="json"),
        from_date=from_date,
        to_date=to_date,
        metrics={
            "portfolio": _metrics_dict(portfolio_metrics),
            "per_symbol": {sr.symbol: _metrics_dict(sr.metrics) for sr in per_symbol},
        },
        data_fingerprint=fingerprint,
    )
    BacktestRunRepository(session).add(run_row)

    for sr in per_symbol:
        for trade in sr.result.trades:
            session.add(
                BacktestTrade(
                    backtest_run_id=run_row.id,
                    instrument_id=sr.instrument_id,
                    detail=_trade_detail(trade, sr.symbol),
                )
            )
    session.flush()

    logger.info(
        "backtest_run_complete",
        backtest_run_id=run_row.id,
        strategy=strategy.name,
        symbols=symbols,
        total_trades=portfolio_metrics.total_trades,
    )
    return MultiSymbolResult(
        backtest_run_id=run_row.id, per_symbol=per_symbol, portfolio_metrics=portfolio_metrics
    )
