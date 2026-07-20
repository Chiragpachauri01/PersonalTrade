from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from personaltrade.core.enums import Interval
from personaltrade.data.store.candles import CandleStore
from personaltrade.data.store.models import Instrument
from personaltrade.execution.paper.quotes import ReplayQuoteSource
from tests.factories import synthetic_candles


@pytest.fixture()
def store(tmp_path: Path) -> CandleStore:
    return CandleStore(tmp_path / "candles")


def _instrument(id_: int = 1, symbol: str = "AAA") -> Instrument:
    inst = Instrument(
        symbol=symbol, exchange="NSE", instrument_key=f"NSE_EQ|{symbol}", tick_size=Decimal("0.05")
    )
    inst.id = id_
    return inst


class TestReplayQuoteSource:
    def test_no_data_returns_none(self, store: CandleStore) -> None:
        source = ReplayQuoteSource(store, Interval.D1)
        assert source.get_ltp(_instrument()) is None

    def test_returns_most_recent_close(self, store: CandleStore) -> None:
        # opens=[100,102,104] -> closes=[101,103,105]; last close is the LTP.
        store.write("AAA", "NSE", Interval.D1, synthetic_candles([100, 102, 104]))
        source = ReplayQuoteSource(store, Interval.D1)
        assert source.get_ltp(_instrument()) == Decimal("105")

    def test_different_symbol_not_confused(self, store: CandleStore) -> None:
        store.write("AAA", "NSE", Interval.D1, synthetic_candles([100]))
        source = ReplayQuoteSource(store, Interval.D1)
        assert source.get_ltp(_instrument(id_=2, symbol="BBB")) is None
