"""Vectorized indicator functions. Pure: series in, series out, no I/O, no clock.

Inputs are float64 candle series (ADR-011) assumed clean (quality-checked, no
NaN). Outputs carry NaN through each indicator's warm-up window. The few
sequential indicators (EMA/RSI/ATR/supertrend) iterate over numpy arrays —
at personal-scale data sizes clarity beats cleverness (Rule: no premature
optimization).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from personaltrade.core.calendar import IST


def _require_period(period: int, minimum: int = 1) -> None:
    if period < minimum:
        raise ValueError(f"period must be >= {minimum}, got {period}")


def sma(close: pd.Series, period: int) -> pd.Series:
    _require_period(period)
    return close.rolling(period).mean().rename(f"sma_{period}")


def ema(close: pd.Series, period: int) -> pd.Series:
    """EMA seeded with the SMA of the first `period` values (TA-Lib convention)."""
    _require_period(period)
    values = close.to_numpy(dtype="float64")
    out = np.full(len(values), np.nan)
    if len(values) >= period:
        alpha = 2.0 / (period + 1.0)
        out[period - 1] = values[:period].mean()
        for i in range(period, len(values)):
            out[i] = alpha * values[i] + (1.0 - alpha) * out[i - 1]
    return pd.Series(out, index=close.index, name=f"ema_{period}")


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder RSI: simple-average seed, then Wilder smoothing."""
    _require_period(period)
    values = close.to_numpy(dtype="float64")
    n = len(values)
    out = np.full(n, np.nan)
    if n >= period + 1:
        deltas = np.diff(values)
        gains = np.clip(deltas, 0.0, None)
        losses = np.clip(-deltas, 0.0, None)
        avg_gain = float(gains[:period].mean())
        avg_loss = float(losses[:period].mean())
        out[period] = _rsi_value(avg_gain, avg_loss)
        for i in range(period + 1, n):
            avg_gain = (avg_gain * (period - 1) + gains[i - 1]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i - 1]) / period
            out[i] = _rsi_value(avg_gain, avg_loss)
    return pd.Series(out, index=close.index, name=f"rsi_{period}")


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0.0:
        return 100.0 if avg_gain > 0.0 else 50.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    """Columns: macd, signal, hist. Signal is an EMA over the valid MACD segment."""
    if not fast < slow:
        raise ValueError(f"fast ({fast}) must be < slow ({slow})")
    _require_period(signal)
    macd_line = (ema(close, fast) - ema(close, slow)).rename("macd")
    valid = macd_line.dropna()
    signal_line = pd.Series(np.nan, index=close.index, name="signal")
    if not valid.empty:
        signal_line.update(ema(valid, signal))
    hist = (macd_line - signal_line).rename("hist")
    return pd.concat([macd_line, signal_line, hist], axis=1)


def bollinger(close: pd.Series, period: int = 20, num_std: float = 2.0) -> pd.DataFrame:
    """Columns: middle, upper, lower. Population std (ddof=0)."""
    _require_period(period, minimum=2)
    middle = close.rolling(period).mean().rename("middle")
    std = close.rolling(period).std(ddof=0)
    upper = (middle + num_std * std).rename("upper")
    lower = (middle - num_std * std).rename("lower")
    return pd.concat([middle, upper, lower], axis=1)


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(
        axis=1
    )
    if len(tr):
        tr.iloc[0] = high.iloc[0] - low.iloc[0]
    return tr.rename("true_range")


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder ATR: simple-average seed over the first `period` true ranges."""
    _require_period(period)
    tr = true_range(high, low, close).to_numpy(dtype="float64")
    n = len(tr)
    out = np.full(n, np.nan)
    if n >= period:
        out[period - 1] = tr[:period].mean()
        for i in range(period, n):
            out[i] = (out[i - 1] * (period - 1) + tr[i]) / period
    return pd.Series(out, index=close.index, name=f"atr_{period}")


def vwap(
    ts: pd.Series, high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series
) -> pd.Series:
    """Session-anchored VWAP: cumulative typical-price volume per IST trading day."""
    typical = (high + low + close) / 3.0
    session = ts.dt.tz_convert(IST).dt.date
    pv = (typical * volume).groupby(session).cumsum()
    cum_vol = volume.groupby(session).cumsum()
    return (pv / cum_vol).rename("vwap")


def obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    """On-balance volume; starts at 0."""
    direction = np.sign(close.diff().fillna(0.0))
    result: pd.Series = (direction * volume).cumsum().rename("obv")
    return result


def supertrend(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 10,
    multiplier: float = 3.0,
) -> pd.DataFrame:
    """Columns: supertrend, direction (+1 up / -1 down). NaN/0 during ATR warm-up.

    Streaming variant is deliberately deferred until a live strategy needs it.
    """
    _require_period(period)
    atr_values = atr(high, low, close, period).to_numpy(dtype="float64")
    h = high.to_numpy(dtype="float64")
    lo = low.to_numpy(dtype="float64")
    c = close.to_numpy(dtype="float64")
    n = len(c)

    st = np.full(n, np.nan)
    direction = np.zeros(n, dtype="int64")
    mid = (h + lo) / 2.0
    upper_basic = mid + multiplier * atr_values
    lower_basic = mid - multiplier * atr_values

    start = period - 1
    if n > start:
        final_upper = upper_basic[start]
        final_lower = lower_basic[start]
        direction[start] = 1 if c[start] > mid[start] else -1
        st[start] = final_lower if direction[start] == 1 else final_upper
        for i in range(start + 1, n):
            final_upper = (
                min(upper_basic[i], final_upper) if c[i - 1] <= final_upper else upper_basic[i]
            )
            final_lower = (
                max(lower_basic[i], final_lower) if c[i - 1] >= final_lower else lower_basic[i]
            )
            if direction[i - 1] == -1:
                direction[i] = 1 if c[i] > final_upper else -1
            else:
                direction[i] = -1 if c[i] < final_lower else 1
            st[i] = final_lower if direction[i] == 1 else final_upper

    return pd.DataFrame({"supertrend": st, "direction": direction}, index=close.index)
