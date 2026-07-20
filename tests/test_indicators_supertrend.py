"""Hand-computed micro golden + property tests for true_range and supertrend."""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import numpy.typing as npt
import pandas as pd
import pytest

from personaltrade.indicators.batch import atr, supertrend, true_range

NAN = float("nan")


def _series(values: Sequence[float] | npt.NDArray[np.float64]) -> pd.Series:
    return pd.Series(values, dtype="float64")


class TestTrueRange:
    def test_hand_golden(self) -> None:
        # see ATR hand golden derivation in test_indicators_batch.py
        high = _series([10, 12, 11, 13, 14])
        low = _series([8, 9, 9, 10, 11])
        close = _series([9, 11, 10, 12, 13])
        result = true_range(high, low, close)
        np.testing.assert_allclose(result.to_numpy(), [2, 3, 2, 3, 3])


class TestSupertrendHandGolden:
    def test_period_3_multiplier_2(self) -> None:
        # ATR(3) = [nan, nan, 2.33333, 2.55556, 2.70370] (see ATR hand golden)
        # mid = (h+l)/2 = [9, 10.5, 10, 11.5, 12.5]
        # upper_basic@2 = 10 + 2*2.33333 = 14.66667; lower_basic@2 = 5.33333
        # start=2: c[2]=10 == mid[2] -> not > -> direction=-1, st=upper_basic=14.66667
        # i=3: c[2]=10<=14.66667 -> final_upper=min(16.61111,14.66667)=14.66667
        #      c[2]=10>=5.33333 -> final_lower=max(6.38889,5.33333)=6.38889
        #      prev dir=-1 -> dir[3] = c[3]=12 > 14.66667? no -> -1; st[3]=14.66667
        # i=4: c[3]=12<=14.66667 -> final_upper=min(17.90741,14.66667)=14.66667
        #      dir[4] = c[4]=13 > 14.66667? no -> -1; st[4]=14.66667
        high = _series([10, 12, 11, 13, 14])
        low = _series([8, 9, 9, 10, 11])
        close = _series([9, 11, 10, 12, 13])
        result = supertrend(high, low, close, period=3, multiplier=2.0)
        np.testing.assert_allclose(
            result["supertrend"].to_numpy(), [NAN, NAN, 14.666667, 14.666667, 14.666667]
        )
        assert list(result["direction"]) == [0, 0, -1, -1, -1]


class TestSupertrendProperties:
    def test_line_equals_lower_when_up_upper_when_down(self) -> None:
        rng = np.random.default_rng(3)
        base = 100 + np.cumsum(rng.normal(0, 1, 60))
        high = _series(base + rng.uniform(0.5, 2.0, 60))
        low = _series(base - rng.uniform(0.5, 2.0, 60))
        close = _series(base + rng.uniform(-0.5, 0.5, 60))
        result = supertrend(high, low, close, period=10, multiplier=3.0)
        atr_values = atr(high, low, close, 10)
        mid = (high + low) / 2.0
        for i in range(10, 60):
            band = 3.0 * atr_values.iloc[i]
            if result["direction"].iloc[i] == 1:
                assert result["supertrend"].iloc[i] <= mid.iloc[i] + band + 1e-6
            else:
                assert result["supertrend"].iloc[i] >= mid.iloc[i] - band - 1e-6

    def test_rejects_nonpositive_period(self) -> None:
        s = _series([1.0, 2.0, 3.0])
        with pytest.raises(ValueError, match="period must be"):
            supertrend(s, s, s, period=0)
