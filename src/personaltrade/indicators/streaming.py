"""Incremental indicator states for live candles (M10+).

Each class mirrors its batch counterpart's arithmetic exactly; equivalence is
enforced by tests (tolerance 1e-9). update() returns None during warm-up —
the streaming analogue of batch NaN.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import numpy as np

from personaltrade.indicators.batch import _require_period, _rsi_value


class SMAState:
    def __init__(self, period: int) -> None:
        _require_period(period)
        self.period = period
        self._buf: deque[float] = deque(maxlen=period)

    def update(self, value: float) -> float | None:
        self._buf.append(value)
        if len(self._buf) < self.period:
            return None
        return float(np.mean(self._buf))


class EMAState:
    """SMA-seeded EMA (matches batch.ema)."""

    def __init__(self, period: int) -> None:
        _require_period(period)
        self.period = period
        self._alpha = 2.0 / (period + 1.0)
        self._seed_buf: list[float] = []
        self._value: float | None = None

    def update(self, value: float) -> float | None:
        if self._value is None:
            self._seed_buf.append(value)
            if len(self._seed_buf) < self.period:
                return None
            self._value = float(np.mean(self._seed_buf))
        else:
            self._value = self._alpha * value + (1.0 - self._alpha) * self._value
        return self._value


class RSIState:
    """Wilder RSI (matches batch.rsi)."""

    def __init__(self, period: int = 14) -> None:
        _require_period(period)
        self.period = period
        self._prev_close: float | None = None
        self._seed_gains: list[float] = []
        self._seed_losses: list[float] = []
        self._avg_gain: float | None = None
        self._avg_loss: float | None = None

    def update(self, close: float) -> float | None:
        if self._prev_close is None:
            self._prev_close = close
            return None
        delta = close - self._prev_close
        self._prev_close = close
        gain = max(delta, 0.0)
        loss = max(-delta, 0.0)

        if self._avg_gain is None or self._avg_loss is None:
            self._seed_gains.append(gain)
            self._seed_losses.append(loss)
            if len(self._seed_gains) < self.period:
                return None
            self._avg_gain = float(np.mean(self._seed_gains))
            self._avg_loss = float(np.mean(self._seed_losses))
        else:
            self._avg_gain = (self._avg_gain * (self.period - 1) + gain) / self.period
            self._avg_loss = (self._avg_loss * (self.period - 1) + loss) / self.period
        return _rsi_value(self._avg_gain, self._avg_loss)


class ATRState:
    """Wilder ATR (matches batch.atr)."""

    def __init__(self, period: int = 14) -> None:
        _require_period(period)
        self.period = period
        self._prev_close: float | None = None
        self._seed_trs: list[float] = []
        self._value: float | None = None

    def update(self, high: float, low: float, close: float) -> float | None:
        if self._prev_close is None:
            tr = high - low
        else:
            tr = max(high - low, abs(high - self._prev_close), abs(low - self._prev_close))
        self._prev_close = close

        if self._value is None:
            self._seed_trs.append(tr)
            if len(self._seed_trs) < self.period:
                return None
            self._value = float(np.mean(self._seed_trs))
        else:
            self._value = (self._value * (self.period - 1) + tr) / self.period
        return self._value


@dataclass(frozen=True)
class MACDValue:
    macd: float
    signal: float | None
    hist: float | None


class MACDState:
    """MACD line available once the slow EMA is warm; signal/hist once its EMA is warm."""

    def __init__(self, fast: int = 12, slow: int = 26, signal: int = 9) -> None:
        if not fast < slow:
            raise ValueError(f"fast ({fast}) must be < slow ({slow})")
        self._fast = EMAState(fast)
        self._slow = EMAState(slow)
        self._signal = EMAState(signal)

    def update(self, close: float) -> MACDValue | None:
        fast_value = self._fast.update(close)
        slow_value = self._slow.update(close)
        if fast_value is None or slow_value is None:
            return None
        macd_value = fast_value - slow_value
        signal_value = self._signal.update(macd_value)
        hist = None if signal_value is None else macd_value - signal_value
        return MACDValue(macd=macd_value, signal=signal_value, hist=hist)


class BollingerState:
    """Returns (middle, upper, lower); population std (matches batch.bollinger)."""

    def __init__(self, period: int = 20, num_std: float = 2.0) -> None:
        _require_period(period, minimum=2)
        self.period = period
        self.num_std = num_std
        self._buf: deque[float] = deque(maxlen=period)

    def update(self, close: float) -> tuple[float, float, float] | None:
        self._buf.append(close)
        if len(self._buf) < self.period:
            return None
        middle = float(np.mean(self._buf))
        band = self.num_std * float(np.std(self._buf))
        return middle, middle + band, middle - band
