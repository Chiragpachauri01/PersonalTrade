"""intelligence/analysis/snapshot.py: `compute_indicators`/`build_market_snapshot`
turn a candle frame into the deterministic inputs a prompt is built from.
"""

from __future__ import annotations

from decimal import Decimal

import pandas as pd

from personaltrade.intelligence.analysis.snapshot import (
    NewsSnapshotItem,
    PositionSnapshot,
    build_market_snapshot,
    compute_indicators,
)
from tests.factories import synthetic_candles


def _warm_frame() -> pd.DataFrame:
    # 45 rows: enough for the slowest warm-up here (MACD signal needs a 26-EMA
    # then a 9-EMA over that, i.e. >= 34 rows before its first valid value).
    return synthetic_candles([100.0 + i for i in range(45)])


class TestComputeIndicators:
    def test_warmed_up_indicators_are_present_and_finite(self) -> None:
        indicators = compute_indicators(_warm_frame())

        for key in ("sma_20", "ema_20", "rsi_14", "atr_14", "macd", "macd_signal", "macd_hist"):
            assert key in indicators
            assert indicators[key] == indicators[key]  # not NaN

    def test_not_yet_warmed_up_indicators_are_omitted(self) -> None:
        short_frame = synthetic_candles([100.0, 101.0, 102.0])
        indicators = compute_indicators(short_frame)
        assert indicators == {}


class TestBuildMarketSnapshot:
    def test_uses_last_row_for_price_and_volume(self) -> None:
        frame = _warm_frame()
        snapshot = build_market_snapshot("AAA", frame, position=None, news=[])

        last = frame.iloc[-1]
        assert snapshot.symbol == "AAA"
        assert snapshot.last_close == Decimal(str(last["close"]))
        assert snapshot.last_volume == int(last["volume"])
        assert snapshot.as_of == last["ts"].to_pydatetime()

    def test_carries_position_and_news_through_unchanged(self) -> None:
        frame = _warm_frame()
        position = PositionSnapshot(qty=5, avg_price=Decimal("100"), unrealized_pnl=Decimal("50"))
        news = [
            NewsSnapshotItem(news_item_id=1, source="s", published_at=None, title="t", body="b")
        ]

        snapshot = build_market_snapshot("AAA", frame, position=position, news=news)

        assert snapshot.position is position
        assert snapshot.news == news
