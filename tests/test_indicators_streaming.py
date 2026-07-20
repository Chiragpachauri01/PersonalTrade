"""Batch-vs-incremental equivalence: streaming states must reproduce the batch
series exactly (within floating tolerance), value by value, including the
warm-up boundary (None <-> NaN).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from personaltrade.indicators.batch import atr, bollinger, ema, macd, rsi, sma
from personaltrade.indicators.streaming import (
    ATRState,
    BollingerState,
    EMAState,
    MACDState,
    RSIState,
    SMAState,
)


def _random_series(n: int, seed: int) -> pd.Series:
    rng = np.random.default_rng(seed)
    return pd.Series(100 + np.cumsum(rng.normal(0, 1, n)))


def _random_hlc(n: int, seed: int) -> tuple[pd.Series, pd.Series, pd.Series]:
    rng = np.random.default_rng(seed)
    base = 100 + np.cumsum(rng.normal(0, 1, n))
    high = pd.Series(base + rng.uniform(0.5, 2.0, n))
    low = pd.Series(base - rng.uniform(0.5, 2.0, n))
    close = pd.Series(base + rng.uniform(-0.5, 0.5, n))
    return high, low, close


@pytest.mark.parametrize("period", [2, 3, 5, 14, 20])
class TestSMAEquivalence:
    def test_matches_batch(self, period: int) -> None:
        series = _random_series(50, seed=period)
        expected = sma(series, period)
        state = SMAState(period)
        for i, value in enumerate(series):
            got = state.update(float(value))
            if pd.isna(expected.iloc[i]):
                assert got is None, f"index {i}: expected warm-up None"
            else:
                assert got == pytest.approx(expected.iloc[i]), f"index {i}"


@pytest.mark.parametrize("period", [2, 3, 5, 14, 20])
class TestEMAEquivalence:
    def test_matches_batch(self, period: int) -> None:
        series = _random_series(50, seed=period + 100)
        expected = ema(series, period)
        state = EMAState(period)
        for i, value in enumerate(series):
            got = state.update(float(value))
            if pd.isna(expected.iloc[i]):
                assert got is None, f"index {i}"
            else:
                assert got == pytest.approx(expected.iloc[i], rel=1e-9), f"index {i}"


@pytest.mark.parametrize("period", [3, 5, 14])
class TestRSIEquivalence:
    def test_matches_batch(self, period: int) -> None:
        series = _random_series(50, seed=period + 200)
        expected = rsi(series, period)
        state = RSIState(period)
        for i, value in enumerate(series):
            got = state.update(float(value))
            if pd.isna(expected.iloc[i]):
                assert got is None, f"index {i}"
            else:
                assert got == pytest.approx(expected.iloc[i], rel=1e-9), f"index {i}"


@pytest.mark.parametrize("period", [3, 7, 14])
class TestATREquivalence:
    def test_matches_batch(self, period: int) -> None:
        high, low, close = _random_hlc(50, seed=period + 300)
        expected = atr(high, low, close, period)
        state = ATRState(period)
        for i in range(len(close)):
            got = state.update(float(high.iloc[i]), float(low.iloc[i]), float(close.iloc[i]))
            if pd.isna(expected.iloc[i]):
                assert got is None, f"index {i}"
            else:
                assert got == pytest.approx(expected.iloc[i], rel=1e-9), f"index {i}"


class TestMACDEquivalence:
    def test_matches_batch(self) -> None:
        series = _random_series(60, seed=42)
        expected = macd(series, fast=12, slow=26, signal=9)
        state = MACDState(12, 26, 9)
        for i, value in enumerate(series):
            got = state.update(float(value))
            row = expected.iloc[i]
            if pd.isna(row["macd"]):
                assert got is None, f"index {i}"
                continue
            assert got is not None
            assert got.macd == pytest.approx(row["macd"], rel=1e-9), f"macd@{i}"
            if pd.isna(row["signal"]):
                assert got.signal is None, f"signal@{i}"
            else:
                assert got.signal == pytest.approx(row["signal"], rel=1e-9), f"signal@{i}"
                assert got.hist == pytest.approx(row["hist"], rel=1e-9), f"hist@{i}"


@pytest.mark.parametrize("period", [3, 10, 20])
class TestBollingerEquivalence:
    def test_matches_batch(self, period: int) -> None:
        series = _random_series(50, seed=period + 400)
        expected = bollinger(series, period, 2.0)
        state = BollingerState(period, 2.0)
        for i, value in enumerate(series):
            got = state.update(float(value))
            if pd.isna(expected["middle"].iloc[i]):
                assert got is None, f"index {i}"
            else:
                assert got is not None
                middle, upper, lower = got
                assert middle == pytest.approx(expected["middle"].iloc[i]), f"middle@{i}"
                assert upper == pytest.approx(expected["upper"].iloc[i]), f"upper@{i}"
                assert lower == pytest.approx(expected["lower"].iloc[i]), f"lower@{i}"
