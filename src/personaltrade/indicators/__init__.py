"""Deterministic technical indicators (Rule 9 — plain numpy/pandas, never an LLM).

Batch functions are pure: candle series in, indicator series out, NaN during
warm-up. Streaming classes produce bit-identical values incrementally (None
during warm-up) — equivalence is enforced by tests.

Conventions (documented here once, tested everywhere):
- EMA seeds with the SMA of the first `period` values (TA-Lib convention).
- RSI and ATR use Wilder smoothing, seeded with a simple average.
- Bollinger uses population std (ddof=0).
- VWAP anchors per IST trading session.
"""

from personaltrade.indicators.batch import (
    atr,
    bollinger,
    ema,
    macd,
    obv,
    rsi,
    sma,
    supertrend,
    true_range,
    vwap,
)
from personaltrade.indicators.streaming import (
    ATRState,
    BollingerState,
    EMAState,
    MACDState,
    RSIState,
    SMAState,
)

__all__ = [
    "ATRState",
    "BollingerState",
    "EMAState",
    "MACDState",
    "RSIState",
    "SMAState",
    "atr",
    "bollinger",
    "ema",
    "macd",
    "obv",
    "rsi",
    "sma",
    "supertrend",
    "true_range",
    "vwap",
]
