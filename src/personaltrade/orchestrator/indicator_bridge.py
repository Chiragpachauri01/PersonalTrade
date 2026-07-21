"""Live/paper indicator computation via incremental streaming states
(indicators/streaming.py), updated once per candle — the live analogue of
backtest/indicator_bridge.py's `BatchIndicatorView`, over the exact same
`IndicatorSpec` declarations a Strategy already provides (Rule 11: one
Strategy contract across backtest, paper, and live).

Only indicator kinds with a streaming state class are supported: sma, ema,
rsi, atr, macd, bollinger. `vwap`/`obv`/`supertrend`/`true_range` have no
streaming implementation yet — a strategy using one of those backtests fine
but cannot run live/paper until a streaming state is added for it.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from typing import Any

from personaltrade.core.errors import PersonalTradeError
from personaltrade.indicators.streaming import (
    ATRState,
    BollingerState,
    EMAState,
    MACDState,
    RSIState,
    SMAState,
)
from personaltrade.strategy.base import IndicatorSpec

#: Generous cap on how much per-indicator history window() can look back over.
#: Strategies in this codebase only ever window() 2-3 bars (crossover checks);
#: 500 comfortably covers more than a full trading day at 1m granularity.
_WINDOW_MAXLEN = 500

_STATE_CLASSES: dict[str, type] = {
    "sma": SMAState,
    "ema": EMAState,
    "rsi": RSIState,
    "atr": ATRState,
    "macd": MACDState,
    "bollinger": BollingerState,
}


class UnknownIndicatorKind(PersonalTradeError):
    """A Strategy declared an IndicatorSpec.kind with no streaming implementation."""


def _expand_names(name: str, kind: str) -> list[str]:
    if kind == "macd":
        return [f"{name}.macd", f"{name}.signal", f"{name}.hist"]
    if kind == "bollinger":
        return [f"{name}.middle", f"{name}.upper", f"{name}.lower"]
    return [name]


def _new_state(name: str, spec: IndicatorSpec) -> Any:
    cls = _STATE_CLASSES.get(spec.kind)
    if cls is None:
        raise UnknownIndicatorKind(
            f"no streaming implementation for indicator kind {spec.kind!r} "
            f"(declared as {name!r}); available: {sorted(_STATE_CLASSES)}"
        )
    return cls(**dict(spec.params))


class LiveIndicatorView:
    """One instance per (instrument, strategy) — `orchestrator/runner.py`'s
    `LiveStrategyRunner` owns it and calls `update()` once per new candle,
    before building that bar's `StrategyContext`."""

    def __init__(self, specs: Mapping[str, IndicatorSpec]) -> None:
        self._specs = dict(specs)
        self._states: dict[str, Any] = {
            name: _new_state(name, spec) for name, spec in self._specs.items()
        }
        self._latest: dict[str, float | None] = {}
        self._history: dict[str, deque[float]] = {}
        for name, spec in self._specs.items():
            for key in _expand_names(name, spec.kind):
                self._latest[key] = None
                self._history[key] = deque(maxlen=_WINDOW_MAXLEN)

    def update(self, *, high: float, low: float, close: float) -> None:
        for name, spec in self._specs.items():
            state = self._states[name]
            result = state.update(high, low, close) if spec.kind == "atr" else state.update(close)
            self._record(name, spec.kind, result)

    def all_warm(self) -> bool:
        """True once every declared indicator has produced at least one value —
        mirrors ADR-015's backtest rule that the engine waits for indicators to
        stop returning NaN regardless of what warmup_bars() itself claims."""
        return all(v is not None for v in self._latest.values())

    def value(self, name: str) -> float | None:
        if name not in self._latest:
            raise KeyError(
                f"indicator {name!r} was not declared by required_indicators() "
                f"(available: {sorted(self._latest)})"
            )
        return self._latest[name]

    def window(self, name: str, n: int) -> list[float]:
        if name not in self._history:
            raise KeyError(
                f"indicator {name!r} was not declared by required_indicators() "
                f"(available: {sorted(self._history)})"
            )
        history = self._history[name]
        values = list(history)[-n:] if n < len(history) else list(history)
        return values

    def _record(self, name: str, kind: str, result: Any) -> None:
        if kind == "macd":
            self._store(f"{name}.macd", None if result is None else result.macd)
            self._store(f"{name}.signal", None if result is None else result.signal)
            self._store(f"{name}.hist", None if result is None else result.hist)
        elif kind == "bollinger":
            if result is None:
                self._store(f"{name}.middle", None)
                self._store(f"{name}.upper", None)
                self._store(f"{name}.lower", None)
            else:
                middle, upper, lower = result
                self._store(f"{name}.middle", middle)
                self._store(f"{name}.upper", upper)
                self._store(f"{name}.lower", lower)
        else:
            self._store(name, result)

    def _store(self, key: str, value: float | None) -> None:
        self._latest[key] = value
        if value is not None:
            self._history[key].append(value)
