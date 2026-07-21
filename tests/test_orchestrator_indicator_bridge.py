"""LiveIndicatorView (ROADMAP M11): dispatch to streaming states, multi-value
expansion (macd/bollinger), warmup gating, and the IndicatorView contract
(value/window). The underlying streaming math itself is already proven
equivalent to batch in tests/test_indicators_streaming.py — this file only
tests the bridge on top of it.
"""

from __future__ import annotations

import pytest

from personaltrade.orchestrator.indicator_bridge import LiveIndicatorView, UnknownIndicatorKind
from personaltrade.strategy.base import IndicatorSpec


class TestSingleValueIndicators:
    def test_sma_updates_and_warms_up(self) -> None:
        view = LiveIndicatorView({"fast": IndicatorSpec("sma", {"period": 3})})
        assert view.value("fast") is None
        view.update(high=101, low=99, close=100)
        view.update(high=102, low=100, close=101)
        assert view.value("fast") is None  # still warming up
        view.update(high=103, low=101, close=102)
        assert view.value("fast") == pytest.approx(101.0)  # (100+101+102)/3

    def test_atr_receives_high_low_close(self) -> None:
        view = LiveIndicatorView({"vol": IndicatorSpec("atr", {"period": 2})})
        view.update(high=102, low=98, close=100)
        view.update(high=103, low=99, close=101)
        assert view.value("vol") == pytest.approx(4.0)  # both bars: tr=high-low=4

    def test_unknown_kind_raises(self) -> None:
        with pytest.raises(UnknownIndicatorKind, match="vwap"):
            LiveIndicatorView({"v": IndicatorSpec("vwap", {})})


class TestMultiValueExpansion:
    def test_macd_expands_to_three_keys(self) -> None:
        view = LiveIndicatorView(
            {"macd": IndicatorSpec("macd", {"fast": 2, "slow": 3, "signal": 2})}
        )
        for close in [100, 102, 104, 106, 108, 110, 112, 108, 104]:
            view.update(high=close + 1, low=close - 1, close=close)
        assert view.value("macd.macd") is not None
        assert view.value("macd.signal") is not None
        assert view.value("macd.hist") is not None

    def test_bollinger_expands_to_three_keys(self) -> None:
        view = LiveIndicatorView({"bb": IndicatorSpec("bollinger", {"period": 3})})
        for close in [100, 101, 99, 102]:
            view.update(high=close + 1, low=close - 1, close=close)
        middle = view.value("bb.middle")
        upper = view.value("bb.upper")
        lower = view.value("bb.lower")
        assert middle is not None
        assert upper is not None
        assert lower is not None
        assert upper > middle > lower


class TestAllWarm:
    def test_false_until_every_indicator_is_ready(self) -> None:
        view = LiveIndicatorView(
            {
                "fast": IndicatorSpec("sma", {"period": 2}),
                "slow": IndicatorSpec("sma", {"period": 5}),
            }
        )
        for close in [100, 101, 102, 103]:
            view.update(high=close, low=close, close=close)
        assert view.all_warm() is False  # "slow" (period 5) still warming
        view.update(high=104, low=104, close=104)
        assert view.all_warm() is True


class TestWindow:
    def test_window_returns_recent_non_none_values_only(self) -> None:
        view = LiveIndicatorView({"fast": IndicatorSpec("sma", {"period": 2})})
        for close in [100, 102, 104, 106]:
            view.update(high=close, low=close, close=close)
        # sma(2) values: None, 101, 103, 105
        assert view.window("fast", 2) == [103.0, 105.0]
        assert view.window("fast", 10) == [101.0, 103.0, 105.0]

    def test_unknown_name_raises_key_error(self) -> None:
        view = LiveIndicatorView({"fast": IndicatorSpec("sma", {"period": 2})})
        with pytest.raises(KeyError, match="fast"):
            view.value("slow")
        with pytest.raises(KeyError, match="fast"):
            view.window("slow", 2)
