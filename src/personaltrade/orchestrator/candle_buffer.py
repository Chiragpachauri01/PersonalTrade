"""Growing candle history for one live (instrument, interval) subscription —
what `StrategyContext.candles` is built from live (ROADMAP M11), the analogue
of the backtest engine's `candles.iloc[: i + 1]` slice.

Rebuilds the DataFrame on every append (O(n) per candle) rather than
incrementally concatenating — simplest correct approach, and cheap at
session scale (a few hundred bars/day at 1m granularity).
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from personaltrade.core.events import CandleReceived
from personaltrade.data.providers.base import empty_candle_frame, normalize_candle_frame


class LiveCandleBuffer:
    def __init__(self) -> None:
        self._rows: list[dict[str, Any]] = []

    def __len__(self) -> int:
        return len(self._rows)

    def append(self, candle: CandleReceived) -> None:
        self._rows.append(
            {
                "ts": candle.ts,
                "open": float(candle.open),
                "high": float(candle.high),
                "low": float(candle.low),
                "close": float(candle.close),
                "volume": candle.volume,
                "oi": 0,
            }
        )

    def frame(self) -> pd.DataFrame:
        if not self._rows:
            return empty_candle_frame()
        return normalize_candle_frame(pd.DataFrame(self._rows))
