from __future__ import annotations

from decimal import Decimal

from personaltrade.data.store.models import Instrument
from personaltrade.execution.paper.quotes import LiveQuoteSource


def _instrument(key: str) -> Instrument:
    inst = Instrument(symbol="X", exchange="NSE", instrument_key=key, tick_size=Decimal("0.05"))
    return inst


class TestLiveQuoteSource:
    def test_no_update_yields_no_quote(self) -> None:
        source = LiveQuoteSource()
        assert source.get_ltp(_instrument("NSE_EQ|X")) is None

    def test_update_then_lookup(self) -> None:
        source = LiveQuoteSource()
        source.update("NSE_EQ|X", Decimal("100.5"))
        assert source.get_ltp(_instrument("NSE_EQ|X")) == Decimal("100.5")

    def test_lookup_keyed_by_instrument_key_not_symbol(self) -> None:
        source = LiveQuoteSource()
        source.update("NSE_EQ|X", Decimal("100.5"))
        assert source.get_ltp(_instrument("NSE_EQ|Y")) is None

    def test_later_update_overwrites(self) -> None:
        source = LiveQuoteSource()
        source.update("NSE_EQ|X", Decimal("100"))
        source.update("NSE_EQ|X", Decimal("105"))
        assert source.get_ltp(_instrument("NSE_EQ|X")) == Decimal("105")
