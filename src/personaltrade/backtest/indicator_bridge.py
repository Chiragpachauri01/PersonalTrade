"""Bridges declarative IndicatorSpecs to precomputed batch series exposed to
strategies through a causal, per-bar IndicatorView.

Precomputing once over the whole input series is safe — not look-ahead —
only because every personaltrade.indicators batch function is provably
causal (rolling windows, forward-recursive EMA/Wilder, session-anchored
cumsum). See personaltrade/indicators/__init__.py and its golden-file tests.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

import pandas as pd

from personaltrade.core.errors import PersonalTradeError
from personaltrade.indicators import batch as ind
from personaltrade.strategy.base import IndicatorSpec


class UnknownIndicatorKind(PersonalTradeError):
    """A Strategy declared an IndicatorSpec.kind with no dispatch entry."""


def _sma(candles: pd.DataFrame, params: dict[str, Any]) -> pd.Series:
    return ind.sma(candles["close"], **params)


def _ema(candles: pd.DataFrame, params: dict[str, Any]) -> pd.Series:
    return ind.ema(candles["close"], **params)


def _rsi(candles: pd.DataFrame, params: dict[str, Any]) -> pd.Series:
    return ind.rsi(candles["close"], **params)


def _atr(candles: pd.DataFrame, params: dict[str, Any]) -> pd.Series:
    return ind.atr(candles["high"], candles["low"], candles["close"], **params)


def _macd(candles: pd.DataFrame, params: dict[str, Any]) -> pd.DataFrame:
    return ind.macd(candles["close"], **params)


def _bollinger(candles: pd.DataFrame, params: dict[str, Any]) -> pd.DataFrame:
    return ind.bollinger(candles["close"], **params)


def _vwap(candles: pd.DataFrame, params: dict[str, Any]) -> pd.Series:
    return ind.vwap(
        candles["ts"], candles["high"], candles["low"], candles["close"], candles["volume"]
    )


def _obv(candles: pd.DataFrame, params: dict[str, Any]) -> pd.Series:
    return ind.obv(candles["close"], candles["volume"])


def _supertrend(candles: pd.DataFrame, params: dict[str, Any]) -> pd.DataFrame:
    return ind.supertrend(candles["high"], candles["low"], candles["close"], **params)


def _true_range(candles: pd.DataFrame, params: dict[str, Any]) -> pd.Series:
    return ind.true_range(candles["high"], candles["low"], candles["close"])


_DISPATCH: dict[str, Callable[[pd.DataFrame, dict[str, Any]], pd.Series | pd.DataFrame]] = {
    "sma": _sma,
    "ema": _ema,
    "rsi": _rsi,
    "atr": _atr,
    "macd": _macd,
    "bollinger": _bollinger,
    "vwap": _vwap,
    "obv": _obv,
    "supertrend": _supertrend,
    "true_range": _true_range,
}


def compute_indicator_set(
    candles: pd.DataFrame, specs: Mapping[str, IndicatorSpec]
) -> dict[str, pd.Series]:
    """Precompute every declared indicator once over the full candle series.

    Multi-column indicators (macd, bollinger, supertrend) expand to
    "<name>.<column>" keys — e.g. name="macd" -> "macd.macd", "macd.signal",
    "macd.hist". All returned series are index-reset to 0..n-1 to align with
    the engine's positional bar indices.
    """
    result: dict[str, pd.Series] = {}
    for name, spec in specs.items():
        fn = _DISPATCH.get(spec.kind)
        if fn is None:
            raise UnknownIndicatorKind(
                f"unknown indicator kind {spec.kind!r} (declared as {name!r}); "
                f"known kinds: {sorted(_DISPATCH)}"
            )
        output = fn(candles, dict(spec.params))
        if isinstance(output, pd.DataFrame):
            for column in output.columns:
                result[f"{name}.{column}"] = output[column].reset_index(drop=True)
        else:
            result[name] = output.reset_index(drop=True)
    return result


def first_all_valid_index(series_by_name: Mapping[str, pd.Series]) -> int:
    """Earliest bar index at which every indicator series is simultaneously non-NaN.

    Returns the series length ("never reached") if any indicator never warms up.
    An empty mapping returns 0 (no indicators to wait on).
    """
    if not series_by_name:
        return 0
    n = len(next(iter(series_by_name.values())))
    valid = pd.Series(True, index=range(n))
    for series in series_by_name.values():
        valid &= series.notna().reset_index(drop=True)
    valid_indices = valid[valid].index
    return int(valid_indices[0]) if len(valid_indices) else n


class BatchIndicatorView:
    """IndicatorView backed by precomputed batch series, bound to one bar index.

    A new instance is built per bar by the engine — there is no way to pass
    a different index in, so a strategy cannot request a future value.
    """

    def __init__(self, series_by_name: Mapping[str, pd.Series], index: int) -> None:
        self._series = series_by_name
        self._index = index

    def value(self, name: str) -> float | None:
        series = self._require(name)
        v = series.iloc[self._index]
        return None if pd.isna(v) else float(v)

    def window(self, name: str, n: int) -> list[float]:
        series = self._require(name)
        lo = max(0, self._index - n + 1)
        values = series.iloc[lo : self._index + 1]
        return [float(x) for x in values if not pd.isna(x)]

    def _require(self, name: str) -> pd.Series:
        series = self._series.get(name)
        if series is None:
            raise KeyError(
                f"indicator {name!r} was not declared by required_indicators() "
                f"(available: {sorted(self._series)})"
            )
        return series
