"""The deterministic inputs an AI analysis call sees (docs/architecture/05-ai-data-flow.md
pipeline diagram) — assembled by the caller (CLI today, Recommendation Engine at
M15), never by this package, which only turns a snapshot into a prompt and a
prompt into a validated result.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

import pandas as pd

from personaltrade.indicators.batch import atr, ema, macd, rsi, sma


@dataclass(frozen=True)
class PositionSnapshot:
    qty: int  # signed: positive long, negative short
    avg_price: Decimal
    unrealized_pnl: Decimal


@dataclass(frozen=True)
class NewsSnapshotItem:
    news_item_id: int
    source: str
    published_at: datetime | None
    title: str
    body: str


@dataclass(frozen=True)
class MarketSnapshot:
    symbol: str
    as_of: datetime
    last_close: Decimal
    last_volume: int
    #: Indicator name -> value, e.g. {"rsi_14": 61.2, "atr_14": 18.4}. `None`
    #: values (not-yet-warmed-up indicators) are omitted before prompting.
    indicators: dict[str, float]
    position: PositionSnapshot | None
    news: list[NewsSnapshotItem] = field(default_factory=list)


def compute_indicators(candles: pd.DataFrame) -> dict[str, float]:
    """SMA/EMA/RSI/MACD/ATR on their standard periods (indicators/batch.py,
    ROADMAP M5), last-row values only, dropping anything still NaN from
    warm-up — a partially-warmed indicator is worse than no indicator in a
    prompt (looks precise, isn't)."""
    close, high, low = candles["close"], candles["high"], candles["low"]
    values: dict[str, float] = {
        "sma_20": sma(close, 20).iloc[-1],
        "ema_20": ema(close, 20).iloc[-1],
        "rsi_14": rsi(close, 14).iloc[-1],
        "atr_14": atr(high, low, close, 14).iloc[-1],
    }
    macd_row = macd(close).iloc[-1]
    values["macd"] = macd_row["macd"]
    values["macd_signal"] = macd_row["signal"]
    values["macd_hist"] = macd_row["hist"]
    return {name: value for name, value in values.items() if not math.isnan(value)}


def build_market_snapshot(
    symbol: str,
    candles: pd.DataFrame,
    *,
    position: PositionSnapshot | None,
    news: list[NewsSnapshotItem],
) -> MarketSnapshot:
    last = candles.iloc[-1]
    return MarketSnapshot(
        symbol=symbol,
        as_of=last["ts"].to_pydatetime(),
        last_close=Decimal(str(last["close"])),
        last_volume=int(last["volume"]),
        indicators=compute_indicators(candles),
        position=position,
        news=news,
    )
