"""The shared Strategy contract (docs/architecture/03-interfaces.md).

Look-ahead safety is structural, not just disciplined: `StrategyContext` is
built fresh per bar and never exposes a way to address a future index —
`candles` is a slice ending at the current bar, and `IndicatorView` has no
index parameter (it always means "as of this context's bar"). A strategy
cannot accidentally peek ahead even if it tried.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar, Protocol, runtime_checkable

import pandas as pd
from pydantic import BaseModel

from personaltrade.core.enums import SignalDirection


@dataclass(frozen=True)
class Signal:
    """A strategy's trading decision at one bar. Sizing/execution happen downstream."""

    direction: SignalDirection
    ref_price: float
    context: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class IndicatorSpec:
    """Declares one indicator a strategy needs; the engine precomputes it once per run.

    `kind` names a batch function in personaltrade.indicators — see the
    dispatch table in backtest/indicator_bridge.py. Multi-column indicators
    (macd, bollinger, supertrend) expose sub-values as "<name>.<column>" on
    the resulting IndicatorView (e.g. requesting kind="macd" under name
    "macd" yields "macd.macd", "macd.signal", "macd.hist").
    """

    kind: str
    params: dict[str, Any] = field(default_factory=dict)


class IndicatorView(Protocol):
    """Causal, per-bar indicator accessor. No method takes an index — there is

    no way to ask for a future value; every method means "as of this bar."
    """

    def value(self, name: str) -> float | None:
        """The named indicator's value at the current bar, or None if still warming up."""
        ...

    def window(self, name: str, n: int) -> list[float]:
        """Up to the last n non-NaN values of the named indicator, ending at the current bar."""
        ...


@dataclass(frozen=True)
class PositionView:
    """Read-only snapshot of the strategy's current position, as of this bar."""

    qty: int  # signed: >0 long, <0 short, 0 flat
    avg_price: float

    @property
    def is_flat(self) -> bool:
        return self.qty == 0

    @property
    def is_long(self) -> bool:
        return self.qty > 0

    @property
    def is_short(self) -> bool:
        return self.qty < 0


FLAT_POSITION = PositionView(qty=0, avg_price=0.0)


@dataclass(frozen=True)
class StrategyContext:
    """Everything on_candle() may see. Never anything beyond the current bar."""

    index: int
    ts: datetime
    candles: pd.DataFrame  # sliced [0 : index+1]; .iloc[-1] is the current bar
    indicators: IndicatorView
    position: PositionView


@runtime_checkable
class Strategy(Protocol):
    """Identical contract in backtest, paper, and live (Rule 11).

    Pure decision function: no I/O, no order placement, no position sizing
    (the risk engine sizes — M8), no wall-clock access (time comes from the
    candle, so backtests are honest).
    """

    name: ClassVar[str]
    params_schema: ClassVar[type[BaseModel]]

    @property
    def params(self) -> BaseModel:
        """The validated instance actually in use — for run persistence/auditing.

        Declared read-only so concrete strategies may narrow the type (e.g.
        `self.params: SMACrossoverParams`) — a plain instance attribute
        satisfies a read-only Protocol property under structural typing,
        covariantly; a mutable Protocol attribute would not.
        """
        ...

    def warmup_bars(self) -> int:
        """Minimum bars of history before on_candle() should be called.

        The engine also independently waits for every required indicator to
        stop returning NaN, so an under-declared warmup_bars() cannot cause
        the strategy to see unready indicator values.
        """
        ...

    def required_indicators(self) -> dict[str, IndicatorSpec]:
        """Indicators this strategy needs, keyed by the name it will look them up under."""
        ...

    def on_candle(self, ctx: StrategyContext) -> Signal | None:
        """Inspect the current bar and emit at most one Signal, or None."""
        ...
