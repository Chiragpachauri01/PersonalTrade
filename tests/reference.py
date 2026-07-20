"""Independent scalar-loop indicator implementations for double-entry testing.

Deliberately written as naive per-element loops over plain Python lists —
a structurally different code path from the vectorized/numpy implementations
in personaltrade.indicators. Agreement between the two is the correctness
evidence (plus hand-computed micro goldens).
"""

from __future__ import annotations

import math

NAN = float("nan")


def ref_sma(values: list[float], period: int) -> list[float]:
    out = [NAN] * len(values)
    for i in range(period - 1, len(values)):
        window = values[i - period + 1 : i + 1]
        out[i] = sum(window) / period
    return out


def ref_ema(values: list[float], period: int) -> list[float]:
    out = [NAN] * len(values)
    if len(values) < period:
        return out
    alpha = 2.0 / (period + 1.0)
    out[period - 1] = sum(values[:period]) / period
    for i in range(period, len(values)):
        out[i] = alpha * values[i] + (1.0 - alpha) * out[i - 1]
    return out


def ref_rsi(values: list[float], period: int) -> list[float]:
    out = [NAN] * len(values)
    if len(values) < period + 1:
        return out
    gains, losses = [], []
    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    out[period] = _ref_rsi_value(avg_gain, avg_loss)
    for i in range(period + 1, len(values)):
        avg_gain = (avg_gain * (period - 1) + gains[i - 1]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i - 1]) / period
        out[i] = _ref_rsi_value(avg_gain, avg_loss)
    return out


def _ref_rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0.0:
        return 100.0 if avg_gain > 0.0 else 50.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def ref_atr(highs: list[float], lows: list[float], closes: list[float], period: int) -> list[float]:
    n = len(closes)
    out = [NAN] * n
    if n < period:
        return out
    trs = []
    for i in range(n):
        if i == 0:
            trs.append(highs[0] - lows[0])
        else:
            trs.append(
                max(
                    highs[i] - lows[i],
                    abs(highs[i] - closes[i - 1]),
                    abs(lows[i] - closes[i - 1]),
                )
            )
    out[period - 1] = sum(trs[:period]) / period
    for i in range(period, n):
        out[i] = (out[i - 1] * (period - 1) + trs[i]) / period
    return out


def ref_bollinger(
    values: list[float], period: int, num_std: float
) -> tuple[list[float], list[float], list[float]]:
    n = len(values)
    middle, upper, lower = [NAN] * n, [NAN] * n, [NAN] * n
    for i in range(period - 1, n):
        window = values[i - period + 1 : i + 1]
        mean = sum(window) / period
        variance = sum((x - mean) ** 2 for x in window) / period
        band = num_std * math.sqrt(variance)
        middle[i], upper[i], lower[i] = mean, mean + band, mean - band
    return middle, upper, lower


def ref_macd(
    values: list[float], fast: int, slow: int, signal: int
) -> tuple[list[float], list[float], list[float]]:
    n = len(values)
    fast_ema = ref_ema(values, fast)
    slow_ema = ref_ema(values, slow)
    macd_line = [
        f - s if not (math.isnan(f) or math.isnan(s)) else NAN
        for f, s in zip(fast_ema, slow_ema, strict=True)
    ]
    valid = [v for v in macd_line if not math.isnan(v)]
    signal_valid = ref_ema(valid, signal)
    signal_line = [NAN] * n
    j = 0
    for i in range(n):
        if not math.isnan(macd_line[i]):
            signal_line[i] = signal_valid[j]
            j += 1
    hist = [
        m - s if not (math.isnan(m) or math.isnan(s)) else NAN
        for m, s in zip(macd_line, signal_line, strict=True)
    ]
    return macd_line, signal_line, hist


def ref_obv(closes: list[float], volumes: list[float]) -> list[float]:
    out = [0.0] * len(closes)
    for i in range(1, len(closes)):
        if closes[i] > closes[i - 1]:
            out[i] = out[i - 1] + volumes[i]
        elif closes[i] < closes[i - 1]:
            out[i] = out[i - 1] - volumes[i]
        else:
            out[i] = out[i - 1]
    return out
