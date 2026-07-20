"""Regression check against a frozen golden file computed from real market data.

tests/golden/reliance_daily_indicators.csv freezes ~3.5 years of real NSE
RELIANCE daily candles (synced in M4) plus every indicator's output at the
time of freezing. This test recomputes indicators from the frozen *input*
columns and asserts the *output* columns are unchanged — a future edit to
batch.py that silently shifts real-world results will fail here, even
though the synthetic tests in test_indicators_batch.py might still pass.

Regenerate deliberately after a reviewed arithmetic change:
    uv run python tests/golden/generate_indicators_golden.py
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from personaltrade.indicators.batch import (
    atr,
    bollinger,
    ema,
    macd,
    obv,
    rsi,
    sma,
    supertrend,
    vwap,
)

GOLDEN_PATH = Path(__file__).parent / "golden" / "reliance_daily_indicators.csv"


@pytest.fixture(scope="module")
def golden() -> pd.DataFrame:
    frame = pd.read_csv(GOLDEN_PATH)
    frame["ts"] = pd.to_datetime(frame["ts"], utc=True)
    return frame


def _assert_column_matches(golden: pd.DataFrame, column: str, computed: pd.Series) -> None:
    np.testing.assert_allclose(
        computed.to_numpy(dtype="float64"),
        golden[column].to_numpy(dtype="float64"),
        rtol=1e-6,
        atol=1e-6,
        equal_nan=True,
        err_msg=f"golden regression: {column} no longer matches the frozen values",
    )


class TestGoldenFileSanity:
    """Fails loudly (not silently 0 rows) if the golden file is ever corrupted."""

    def test_has_expected_shape(self, golden: pd.DataFrame) -> None:
        assert len(golden) > 800
        assert golden["ts"].is_monotonic_increasing
        assert not golden[["open", "high", "low", "close", "volume"]].isna().any().any()

    def test_warm_up_regions_are_nan(self, golden: pd.DataFrame) -> None:
        assert golden["sma_20"].iloc[:19].isna().all()
        assert golden["sma_20"].iloc[19:].notna().all()
        assert golden["rsi_14"].iloc[:14].isna().all()


class TestIndicatorsMatchGoldenOutputs:
    def test_sma(self, golden: pd.DataFrame) -> None:
        _assert_column_matches(golden, "sma_20", sma(golden["close"], 20))

    def test_ema(self, golden: pd.DataFrame) -> None:
        _assert_column_matches(golden, "ema_20", ema(golden["close"], 20))

    def test_rsi(self, golden: pd.DataFrame) -> None:
        _assert_column_matches(golden, "rsi_14", rsi(golden["close"], 14))

    def test_atr(self, golden: pd.DataFrame) -> None:
        computed = atr(golden["high"], golden["low"], golden["close"], 14)
        _assert_column_matches(golden, "atr_14", computed)

    def test_macd(self, golden: pd.DataFrame) -> None:
        result = macd(golden["close"])
        _assert_column_matches(golden, "macd", result["macd"])
        _assert_column_matches(golden, "macd_signal", result["signal"])
        _assert_column_matches(golden, "macd_hist", result["hist"])

    def test_bollinger(self, golden: pd.DataFrame) -> None:
        result = bollinger(golden["close"], 20, 2.0)
        _assert_column_matches(golden, "bb_middle", result["middle"])
        _assert_column_matches(golden, "bb_upper", result["upper"])
        _assert_column_matches(golden, "bb_lower", result["lower"])

    def test_vwap_equals_typical_price_for_daily_bars(self, golden: pd.DataFrame) -> None:
        # Each daily candle is its own IST session, so session-anchored VWAP
        # degenerates to that single bar's typical price — a real edge case
        # this golden validates end-to-end (session grouping actually works).
        computed = vwap(
            golden["ts"], golden["high"], golden["low"], golden["close"], golden["volume"]
        )
        typical = (golden["high"] + golden["low"] + golden["close"]) / 3.0
        _assert_column_matches(golden, "vwap", computed)
        np.testing.assert_allclose(computed.to_numpy(), typical.to_numpy(), rtol=1e-9)

    def test_obv(self, golden: pd.DataFrame) -> None:
        _assert_column_matches(golden, "obv", obv(golden["close"], golden["volume"]))

    def test_supertrend(self, golden: pd.DataFrame) -> None:
        result = supertrend(golden["high"], golden["low"], golden["close"], 10, 3.0)
        _assert_column_matches(golden, "supertrend", result["supertrend"])
        np.testing.assert_array_equal(
            result["direction"].to_numpy(), golden["supertrend_dir"].to_numpy()
        )
