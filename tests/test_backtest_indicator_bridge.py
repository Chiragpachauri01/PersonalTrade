from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from personaltrade.backtest.indicator_bridge import (
    BatchIndicatorView,
    UnknownIndicatorKind,
    compute_indicator_set,
    first_all_valid_index,
)
from personaltrade.indicators import batch as ind
from personaltrade.strategy.base import IndicatorSpec
from tests.factories import daily_frame


class TestComputeIndicatorSetSingleColumn:
    def test_sma_matches_direct_call(self) -> None:
        frame = daily_frame()
        result = compute_indicator_set(frame, {"fast": IndicatorSpec("sma", {"period": 5})})
        expected = ind.sma(frame["close"], 5)
        assert list(result) == ["fast"]
        np.testing.assert_allclose(result["fast"].to_numpy(), expected.to_numpy(), equal_nan=True)

    def test_multiple_named_indicators(self) -> None:
        frame = daily_frame()
        specs = {
            "fast": IndicatorSpec("sma", {"period": 3}),
            "slow": IndicatorSpec("ema", {"period": 5}),
            "strength": IndicatorSpec("rsi", {"period": 4}),
        }
        result = compute_indicator_set(frame, specs)
        assert set(result) == {"fast", "slow", "strength"}
        np.testing.assert_allclose(
            result["fast"].to_numpy(), ind.sma(frame["close"], 3).to_numpy(), equal_nan=True
        )

    def test_atr_and_vwap_and_obv(self) -> None:
        frame = daily_frame()
        specs = {
            "a": IndicatorSpec("atr", {"period": 3}),
            "v": IndicatorSpec("vwap"),
            "o": IndicatorSpec("obv"),
            "tr": IndicatorSpec("true_range"),
        }
        result = compute_indicator_set(frame, specs)
        np.testing.assert_allclose(
            result["a"].to_numpy(),
            ind.atr(frame["high"], frame["low"], frame["close"], 3).to_numpy(),
            equal_nan=True,
        )
        np.testing.assert_allclose(
            result["o"].to_numpy(), ind.obv(frame["close"], frame["volume"]).to_numpy()
        )

    def test_unknown_kind_raises(self) -> None:
        with pytest.raises(UnknownIndicatorKind, match="banana"):
            compute_indicator_set(daily_frame(), {"x": IndicatorSpec("banana")})


class TestComputeIndicatorSetMultiColumn:
    def test_macd_expands_to_dotted_keys(self) -> None:
        frame = daily_frame()
        result = compute_indicator_set(
            frame, {"macd": IndicatorSpec("macd", {"fast": 2, "slow": 3, "signal": 2})}
        )
        assert set(result) == {"macd.macd", "macd.signal", "macd.hist"}
        expected = ind.macd(frame["close"], fast=2, slow=3, signal=2)
        np.testing.assert_allclose(
            result["macd.macd"].to_numpy(), expected["macd"].to_numpy(), equal_nan=True
        )

    def test_bollinger_expands_to_dotted_keys(self) -> None:
        frame = daily_frame()
        result = compute_indicator_set(frame, {"bb": IndicatorSpec("bollinger", {"period": 3})})
        assert set(result) == {"bb.middle", "bb.upper", "bb.lower"}

    def test_supertrend_expands_to_dotted_keys(self) -> None:
        frame = daily_frame()
        result = compute_indicator_set(
            frame, {"st": IndicatorSpec("supertrend", {"period": 3, "multiplier": 2.0})}
        )
        assert set(result) == {"st.supertrend", "st.direction"}

    def test_same_kind_different_names_no_collision(self) -> None:
        frame = daily_frame()
        specs = {
            "macd_a": IndicatorSpec("macd", {"fast": 2, "slow": 3, "signal": 2}),
            "macd_b": IndicatorSpec("macd", {"fast": 3, "slow": 5, "signal": 2}),
        }
        result = compute_indicator_set(frame, specs)
        assert {
            "macd_a.macd",
            "macd_a.signal",
            "macd_a.hist",
            "macd_b.macd",
            "macd_b.signal",
            "macd_b.hist",
        } <= set(result)
        assert not result["macd_a.macd"].equals(result["macd_b.macd"])


class TestFirstAllValidIndex:
    def test_empty_mapping_is_zero(self) -> None:
        assert first_all_valid_index({}) == 0

    def test_single_series_warmup(self) -> None:
        frame = daily_frame()  # 13 rows
        series = {"fast": ind.sma(frame["close"], 5).reset_index(drop=True)}
        assert first_all_valid_index(series) == 4  # sma(5) first valid at index 4

    def test_max_of_multiple_warmups_wins(self) -> None:
        frame = daily_frame()
        series = {
            "fast": ind.sma(frame["close"], 5).reset_index(drop=True),
            "slow": ind.sma(frame["close"], 8).reset_index(drop=True),
        }
        assert first_all_valid_index(series) == 7  # sma(8) warms up later

    def test_never_warms_up_returns_length(self) -> None:
        all_nan = pd.Series([float("nan")] * 10)
        assert first_all_valid_index({"x": all_nan}) == 10


class TestBatchIndicatorView:
    def test_value_returns_none_during_warmup(self) -> None:
        frame = daily_frame()
        series = {"fast": ind.sma(frame["close"], 5).reset_index(drop=True)}
        assert BatchIndicatorView(series, 0).value("fast") is None
        assert BatchIndicatorView(series, 4).value("fast") is not None

    def test_value_matches_series_at_index(self) -> None:
        frame = daily_frame()
        expected = ind.sma(frame["close"], 5).reset_index(drop=True)
        series = {"fast": expected}
        view = BatchIndicatorView(series, 6)
        assert view.value("fast") == pytest.approx(float(expected.iloc[6]))

    def test_window_returns_trailing_non_nan_values(self) -> None:
        frame = daily_frame()
        expected = ind.sma(frame["close"], 5).reset_index(drop=True)
        series = {"fast": expected}
        window = BatchIndicatorView(series, 6).window("fast", 3)
        assert window == [pytest.approx(float(v)) for v in expected.iloc[4:7]]

    def test_window_shorter_than_n_near_start_of_valid_region(self) -> None:
        frame = daily_frame()
        expected = ind.sma(frame["close"], 5).reset_index(drop=True)
        series = {"fast": expected}
        # at index 4 (first valid point), a window of 3 can only find 1 non-NaN value
        assert BatchIndicatorView(series, 4).window("fast", 3) == [
            pytest.approx(float(expected.iloc[4]))
        ]

    def test_undeclared_name_raises_keyerror(self) -> None:
        view = BatchIndicatorView({"fast": pd.Series([1.0, 2.0])}, 0)
        with pytest.raises(KeyError, match="not declared"):
            view.value("slow")
        with pytest.raises(KeyError, match="not declared"):
            view.window("slow", 3)

    def test_causal_bridge_matches_full_series_value_at_same_index(self) -> None:
        """The bridge itself introduces no look-ahead: precomputing over the whole
        frame and reading index i gives the same value as computing over a frame
        truncated at i (relying on batch.sma's own causality, proven in M5)."""
        frame = daily_frame()
        full = compute_indicator_set(frame, {"s": IndicatorSpec("sma", {"period": 4})})
        truncated = compute_indicator_set(
            frame.iloc[:7], {"s": IndicatorSpec("sma", {"period": 4})}
        )
        assert BatchIndicatorView(full, 6).value("s") == pytest.approx(
            BatchIndicatorView(truncated, 6).value("s")
        )
