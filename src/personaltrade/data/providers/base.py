"""MarketDataProvider interface + provider-neutral market-data types.

Candle frames are the one place money is float64, not Decimal (ADR-011):
market-data arrays feed vectorized analytics; transactional money stays Decimal.
Frame contract: columns CANDLE_COLUMNS, ts tz-aware UTC, sorted ascending, unique.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol

import pandas as pd

from personaltrade.core.enums import Interval
from personaltrade.core.errors import PersonalTradeError

CANDLE_COLUMNS = ["ts", "open", "high", "low", "close", "volume", "oi"]


class MarketDataError(PersonalTradeError):
    """A market-data provider failed (transport, API error, malformed payload)."""


@dataclass(frozen=True)
class InstrumentInfo:
    """Provider-neutral instrument master record (equities)."""

    symbol: str
    exchange: str
    isin: str
    instrument_key: str
    name: str
    tick_size: Decimal
    lot_size: int


class MarketDataProvider(Protocol):
    """Historical market data. Live streaming arrives in M10 (stream_quotes)."""

    def get_instruments(self, exchange: str = "NSE") -> list[InstrumentInfo]:
        """Fetch the instrument master for an exchange (equities only)."""
        ...

    def get_historical_candles(
        self,
        instrument_key: str,
        interval: Interval,
        from_date: date,
        to_date: date,
    ) -> pd.DataFrame:
        """OHLCV candles for [from_date, to_date] IST, per the frame contract above."""
        ...


def empty_candle_frame() -> pd.DataFrame:
    frame = pd.DataFrame(columns=CANDLE_COLUMNS)
    frame["ts"] = pd.to_datetime(frame["ts"], utc=True)
    return frame


def normalize_candle_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Enforce the frame contract: sorted ascending, unique ts, canonical dtypes."""
    if frame.empty:
        return empty_candle_frame()
    out = frame[CANDLE_COLUMNS].copy()
    out["ts"] = pd.to_datetime(out["ts"], utc=True)
    for col in ("open", "high", "low", "close"):
        out[col] = out[col].astype("float64")
    for col in ("volume", "oi"):
        out[col] = out[col].astype("int64")
    out = out.drop_duplicates(subset="ts", keep="last").sort_values("ts")
    return out.reset_index(drop=True)
