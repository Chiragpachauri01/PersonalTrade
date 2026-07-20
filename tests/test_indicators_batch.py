"""Hand-computed micro goldens + independent-reference cross-checks.

Every hand golden shows its derivation in a comment so a reviewer can verify
it with a calculator without trusting the code under test.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import numpy.typing as npt
import pandas as pd
import pytest

from personaltrade.indicators.batch import atr, bollinger, ema, macd, obv, rsi, sma, vwap
from tests.reference import (
    ref_atr,
    ref_bollinger,
    ref_ema,
    ref_macd,
    ref_obv,
    ref_rsi,
    ref_sma,
)

NAN = float("nan")


def _series(values: Sequence[float] | npt.NDArray[np.float64]) -> pd.Series:
    return pd.Series(values, dtype="float64")


def _assert_close(actual: pd.Series | np.ndarray, expected: list[float]) -> None:
    np.testing.assert_allclose(np.asarray(actual, dtype="float64"), expected, rtol=1e-6, atol=1e-9)


class TestSMAHandGolden:
    def test_period_3(self) -> None:
        # [1,2,3,4,5,6], period 3: rolling means 2,3,4,5
        result = sma(_series([1, 2, 3, 4, 5, 6]), 3)
        _assert_close(result, [NAN, NAN, 2, 3, 4, 5])

    def test_rejects_nonpositive_period(self) -> None:
        with pytest.raises(ValueError, match="period must be"):
            sma(_series([1, 2, 3]), 0)


class TestEMAHandGolden:
    def test_period_3_seeded_with_sma(self) -> None:
        # values [10,20,30,25,15,40,35], period 3, alpha=0.5
        # seed@2 = mean(10,20,30) = 20
        # @3 = .5*25 + .5*20   = 22.5
        # @4 = .5*15 + .5*22.5 = 18.75
        # @5 = .5*40 + .5*18.75= 29.375
        # @6 = .5*35 + .5*29.375=32.1875
        result = ema(_series([10, 20, 30, 25, 15, 40, 35]), 3)
        _assert_close(result, [NAN, NAN, 20, 22.5, 18.75, 29.375, 32.1875])


class TestRSIHandGolden:
    def test_period_3_two_exact_points(self) -> None:
        # values [10,20,30,25,15,40,35], period 3
        # deltas: +10,+10,-5,-10,+25,-5
        # seed gains[:3]=[10,10,0] avg=6.6667; losses[:3]=[0,0,5] avg=1.6667
        # RSI@3 = 100 - 100/(1 + (20/3)/(5/3)) = 100 - 100/5 = 80.0  (exact: 20/5=4)
        # @4: gain=0,loss=10 -> avg_gain=(6.6667*2+0)/3=4.4444; avg_loss=(1.6667*2+10)/3=4.4444
        #     equal averages -> RSI = 50.0 exactly
        result = rsi(_series([10, 20, 30, 25, 15, 40, 35]), 3)
        assert result.iloc[3] == pytest.approx(80.0)
        assert result.iloc[4] == pytest.approx(50.0)
        assert result.iloc[:3].isna().all()

    def test_all_gains_is_100(self) -> None:
        result = rsi(_series([1, 2, 3, 4, 5]), 3)
        assert result.iloc[3] == pytest.approx(100.0)


class TestATRHandGolden:
    def test_period_3(self) -> None:
        # TR[0]=hi-lo=10-8=2
        # TR[1]=max(12-9=3, |12-9|=3, |9-9|=0)=3
        # TR[2]=max(11-9=2, |11-11|=0, |9-11|=2)=2
        # TR[3]=max(13-10=3, |13-10|=3, |10-10|=0)=3
        # TR[4]=max(14-11=3, |14-12|=2, |11-12|=1)=3
        # seed@2 = mean(2,3,2) = 2.33333
        # @3 = (2.33333*2+3)/3 = 2.55556
        # @4 = (2.55556*2+3)/3 = 2.70370
        high = _series([10, 12, 11, 13, 14])
        low = _series([8, 9, 9, 10, 11])
        close = _series([9, 11, 10, 12, 13])
        result = atr(high, low, close, 3)
        _assert_close(result, [NAN, NAN, 2.333333, 2.555556, 2.703704])


class TestBollingerHandGolden:
    def test_period_3_population_std(self) -> None:
        # window@2=[10,20,30]: mean=20, var=(100+0+100)/3=66.6667, std=8.16497
        # window@3=[20,30,25]: mean=25, var=(25+25+0)/3=16.6667, std=4.08248
        close = _series([10, 20, 30, 25, 15])
        result = bollinger(close, 3, 2.0)
        assert result["middle"].iloc[2] == pytest.approx(20.0)
        assert result["upper"].iloc[2] == pytest.approx(20 + 2 * 8.164966)
        assert result["lower"].iloc[2] == pytest.approx(20 - 2 * 8.164966)
        assert result["middle"].iloc[3] == pytest.approx(25.0)
        assert result["upper"].iloc[3] == pytest.approx(25 + 2 * 4.082483)


class TestMACDHandGolden:
    def test_fast2_slow3_signal2(self) -> None:
        # fast EMA(2), alpha=2/3: seed@1=mean(10,20)=15; @2=.6667*30+.3333*15=25;
        #   @3=.6667*25+.3333*25=25; @4=.6667*15+.3333*25=18.3333
        # slow EMA(3), alpha=.5: seed@2=20; @3=22.5; @4=18.75
        # macd = fast-slow: @2=5, @3=2.5, @4=-0.41667
        # signal EMA(2) over valid macd [5,2.5,-0.41667]:
        #   seed(pos1)=mean(5,2.5)=3.75 -> aligns to idx3
        #   pos2 = .6667*(-0.41667)+.3333*3.75 = 0.97222 -> aligns to idx4
        # hist@3 = 2.5-3.75=-1.25; hist@4 = -0.41667-0.97222=-1.38889
        close = _series([10, 20, 30, 25, 15])
        result = macd(close, fast=2, slow=3, signal=2)
        assert result["macd"].iloc[3] == pytest.approx(2.5)
        assert result["signal"].iloc[3] == pytest.approx(3.75)
        assert result["hist"].iloc[3] == pytest.approx(-1.25)
        assert result["macd"].iloc[4] == pytest.approx(-0.416667)
        assert result["signal"].iloc[4] == pytest.approx(0.972222)
        assert result["hist"].iloc[4] == pytest.approx(-1.388889)

    def test_fast_must_be_less_than_slow(self) -> None:
        with pytest.raises(ValueError, match="must be <"):
            macd(_series([1, 2, 3]), fast=26, slow=12)


class TestVWAPHandGolden:
    def test_single_session(self) -> None:
        # typical = (h+l+c)/3 = [9, 10.66667, 10]; pv = typical*vol
        # cum_pv = [900, 3033.333, 4533.333]; cum_vol = [100, 300, 450]
        # vwap = [9, 10.11111, 10.07407]
        ts = pd.Series(
            pd.to_datetime(["2026-07-01 04:00", "2026-07-01 05:00", "2026-07-01 06:00"], utc=True)
        )
        high = _series([10, 12, 11])
        low = _series([8, 9, 9])
        close = _series([9, 11, 10])
        volume = _series([100, 200, 150])
        result = vwap(ts, high, low, close, volume)
        _assert_close(result, [9.0, 10.111111, 10.074074])

    def test_new_session_resets(self) -> None:
        ts = pd.Series(
            pd.to_datetime(["2026-07-01 04:00", "2026-07-01 05:00", "2026-07-02 04:00"], utc=True)
        )
        high = _series([10, 12, 20])
        low = _series([8, 9, 18])
        close = _series([9, 11, 19])
        volume = _series([100, 200, 300])
        result = vwap(ts, high, low, close, volume)
        # third row is a new IST session -> vwap resets to that row's typical price
        assert result.iloc[2] == pytest.approx((20 + 18 + 19) / 3)


class TestOBVHandGolden:
    def test_up_down_flat(self) -> None:
        close = _series([10, 11, 10, 12, 12, 11])
        volume = _series([100, 200, 150, 300, 120, 90])
        result = obv(close, volume)
        _assert_close(result, [0, 200, 50, 350, 350, 260])


class TestAgainstIndependentReference:
    """Cross-check the vectorized implementation against a naive scalar loop."""

    @pytest.mark.parametrize("period", [2, 3, 5, 14])
    @pytest.mark.parametrize(
        "values",
        [
            [
                10.0,
                20.0,
                30.0,
                25.0,
                15.0,
                40.0,
                35.0,
                20.0,
                45.0,
                50.0,
                48.0,
                46.0,
                44.0,
                42.0,
                40.0,
            ],
            list(np.linspace(100, 200, 30)),
            [50.0 + 10.0 * np.sin(i / 3.0) for i in range(40)],
        ],
    )
    def test_sma_ema_rsi(self, values: list[float], period: int) -> None:
        series = _series(values)
        _assert_close(sma(series, period), ref_sma(values, period))
        _assert_close(ema(series, period), ref_ema(values, period))
        if len(values) > period:
            _assert_close(rsi(series, period), ref_rsi(values, period))

    @pytest.mark.parametrize("period", [3, 7, 14])
    def test_atr(self, period: int) -> None:
        rng = np.random.default_rng(42)
        base = 100 + np.cumsum(rng.normal(0, 1, 50))
        high = base + rng.uniform(0.5, 2.0, 50)
        low = base - rng.uniform(0.5, 2.0, 50)
        close = base + rng.uniform(-0.5, 0.5, 50)
        result = atr(_series(high), _series(low), _series(close), period)
        expected = ref_atr(
            [float(x) for x in high], [float(x) for x in low], [float(x) for x in close], period
        )
        _assert_close(result, expected)

    @pytest.mark.parametrize("period", [2, 5, 10])
    def test_bollinger(self, period: int) -> None:
        values = list(50 + 5 * np.sin(np.arange(40) / 2.0))
        m, u, lo = ref_bollinger(values, period, 2.0)
        result = bollinger(_series(values), period, 2.0)
        _assert_close(result["middle"], m)
        _assert_close(result["upper"], u)
        _assert_close(result["lower"], lo)

    def test_macd_default_params(self) -> None:
        values = list(100 + 3 * np.cos(np.arange(60) / 4.0))
        m, s, h = ref_macd(values, 12, 26, 9)
        result = macd(_series(values))
        _assert_close(result["macd"], m)
        _assert_close(result["signal"], s)
        _assert_close(result["hist"], h)

    def test_obv(self) -> None:
        rng = np.random.default_rng(7)
        closes = [float(x) for x in 100 + np.cumsum(rng.normal(0, 1, 30))]
        volumes = [float(x) for x in rng.integers(100, 1000, 30)]
        _assert_close(obv(_series(closes), _series(volumes)), ref_obv(closes, volumes))
