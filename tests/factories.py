"""Shared test-data builders."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import ClassVar

import pandas as pd
from pydantic import BaseModel

from personaltrade.core.enums import SignalDirection
from personaltrade.data.providers.base import normalize_candle_frame
from personaltrade.data.store.models import Instrument
from personaltrade.strategy.base import IndicatorSpec, Signal, StrategyContext

#: Real RELIANCE daily candles from the Upstox v3 API (captured 2026-07-19),
#: as returned on the wire: newest-first, IST offsets, [ts, o, h, l, c, vol, oi].
RELIANCE_DAILY_CANDLES: list[list[object]] = [
    ["2026-07-17T00:00:00+05:30", 1300.0, 1330.3, 1296.1, 1327.2, 18302218, 0],
    ["2026-07-16T00:00:00+05:30", 1310.1, 1315.0, 1296.4, 1299.6, 12645128, 0],
    ["2026-07-15T00:00:00+05:30", 1294.1, 1312.6, 1294.1, 1309.5, 11456209, 0],
    ["2026-07-14T00:00:00+05:30", 1299.0, 1305.0, 1291.3, 1295.7, 9834511, 0],
    ["2026-07-13T00:00:00+05:30", 1305.4, 1311.5, 1297.0, 1299.9, 8672233, 0],
    ["2026-07-10T00:00:00+05:30", 1312.0, 1318.9, 1302.5, 1306.2, 10233417, 0],
    ["2026-07-09T00:00:00+05:30", 1308.7, 1316.4, 1305.1, 1312.9, 9128840, 0],
    ["2026-07-08T00:00:00+05:30", 1301.2, 1312.0, 1298.8, 1308.4, 8556120, 0],
    ["2026-07-07T00:00:00+05:30", 1296.5, 1305.9, 1293.2, 1300.8, 7998454, 0],
    ["2026-07-06T00:00:00+05:30", 1303.8, 1308.2, 1294.7, 1297.3, 8110236, 0],
    ["2026-07-03T00:00:00+05:30", 1310.2, 1314.6, 1301.9, 1305.5, 7684521, 0],
    ["2026-07-02T00:00:00+05:30", 1305.0, 1316.8, 1303.4, 1311.7, 8291374, 0],
    ["2026-07-01T00:00:00+05:30", 1298.9, 1312.2, 1296.5, 1308.0, 7001401, 0],
]


def wire_candles_payload(candles: list[list[object]]) -> dict[str, object]:
    """The exact envelope the Upstox v3 historical endpoint returns."""
    return {"status": "success", "data": {"candles": candles}}


def daily_frame(candles: list[list[object]] | None = None) -> pd.DataFrame:
    """A normalized candle frame (UTC, ascending) from wire-format rows."""
    rows = candles if candles is not None else RELIANCE_DAILY_CANDLES
    frame = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume", "oi"])
    frame["ts"] = pd.to_datetime(frame["ts"], utc=True)
    return normalize_candle_frame(frame)


def synthetic_candles(
    opens: list[float], start: datetime | None = None, volume: int = 1000
) -> pd.DataFrame:
    """Clean, hand-traceable OHLCV: high=open+2, low=open-2, close=open+1.

    Consecutive daily UTC timestamps. Used by backtest engine tests where the
    exact fill/equity arithmetic needs to be reconstructible by hand.
    """
    base = start or datetime(2026, 1, 1, tzinfo=UTC)
    rows = [
        {
            "ts": base + timedelta(days=i),
            "open": o,
            "high": o + 2,
            "low": o - 2,
            "close": o + 1,
            "volume": volume,
            "oi": 0,
        }
        for i, o in enumerate(opens)
    ]
    return normalize_candle_frame(pd.DataFrame(rows))


class _EmptyParams(BaseModel):
    model_config = {"extra": "forbid"}


class _ScriptedParams(BaseModel):
    """Wraps the schedule as real params so ScriptedStrategy.params is a
    genuine BaseModel (satisfying the Strategy protocol) and so
    `type(strategy)(strategy.params)` — the reconstruction backtest/run.py
    uses per symbol — works on this test double too."""

    model_config = {"arbitrary_types_allowed": True}
    schedule: dict[int, Signal] = {}


class ScriptedStrategy:
    """Emits exactly the Signal scheduled for a given bar index, nothing else.

    Bypasses indicator/crossover logic entirely so backtest-engine tests can
    dictate precisely which bar emits which signal, for hand-traceable fills.
    Accepts a bare dict (the usual call convention) or a _ScriptedParams
    instance (what the run.py per-symbol reconstruction passes).
    """

    name: ClassVar[str] = "scripted"
    params_schema: ClassVar[type[BaseModel]] = _ScriptedParams

    def __init__(self, schedule: dict[int, Signal] | _ScriptedParams) -> None:
        self.params = (
            schedule
            if isinstance(schedule, _ScriptedParams)
            else _ScriptedParams(schedule=schedule)
        )
        self.schedule = self.params.schedule

    def clone(self) -> ScriptedStrategy:
        return ScriptedStrategy(self.params)

    def warmup_bars(self) -> int:
        return 0

    def required_indicators(self) -> dict[str, IndicatorSpec]:
        return {}

    def on_candle(self, ctx: StrategyContext) -> Signal | None:
        return self.schedule.get(ctx.index)


class FixedQtySizer:
    """PositionSizer test double: always proposes the same quantity."""

    def __init__(self, qty: int) -> None:
        self.qty = qty

    def size(self, equity: Decimal, price: Decimal) -> int:
        return self.qty


class LeakyOnceStrategy:
    """Deliberately non-self-healing stateful strategy: emits LONG on the very
    first `on_candle` call this INSTANCE ever receives, and never again —
    with no reset logic of any kind (unlike ema_atr_stop.py's `is_flat`
    self-heal).

    Exists purely to test the orchestration-level guarantee in
    backtest/run.py (fresh strategy instance per symbol): if that guarantee
    ever regressed to reusing one instance across symbols, only the FIRST
    symbol in a multi-symbol run would ever see call_count==1 and emit its
    entry signal — every symbol after it would silently get no signal at
    all. A real strategy that forgets to reset on flat would fail exactly
    this way in production.
    """

    name: ClassVar[str] = "leaky_once"
    params_schema: ClassVar[type[BaseModel]] = _EmptyParams

    def __init__(self, params: _EmptyParams | None = None) -> None:
        self.params = params or _EmptyParams()
        self.call_count = 0

    def clone(self) -> LeakyOnceStrategy:
        return LeakyOnceStrategy(self.params)

    def warmup_bars(self) -> int:
        return 0

    def required_indicators(self) -> dict[str, IndicatorSpec]:
        return {}

    def on_candle(self, ctx: StrategyContext) -> Signal | None:
        self.call_count += 1
        if self.call_count == 1:
            close = float(ctx.candles["close"].iloc[-1])
            return Signal(SignalDirection.LONG, close, {})
        return None


class FakeQuoteSource:
    """QuoteSource test double: an explicit, mutable price table, so a test can
    change "the market" between calls (e.g. to make a resting limit order
    marketable) without needing real candle data on disk."""

    def __init__(self, prices: dict[int, Decimal] | None = None) -> None:
        self.prices = prices or {}

    def get_ltp(self, instrument: Instrument) -> Decimal | None:
        return self.prices.get(instrument.id)


class ManualClock:
    """Clock test double: advances only when told to, so fill/latency timestamps
    are exactly predictable instead of racing the real wall clock."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 1, 1, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, **kwargs: float) -> None:
        self._now += timedelta(**kwargs)
