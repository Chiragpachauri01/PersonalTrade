"""QuoteSource implementations for the Paper Broker.

`ReplayQuoteSource` was the only one before M10 (Live Market Data Feed): it
returns the most recently *synced* candle's close as a stand-in LTP — coarse
(daily-bar granularity) but genuinely correct, a real reference price, not a
fake one. `LiveQuoteSource` (M11) supersedes it during a live/paper trading
session: the Orchestrator feeds it each `CandleReceived` event's close as it
arrives, so the Paper Broker prices fills off the session's actual last trade
instead of yesterday's close — exactly the seam M10's ADR-019 built this
Protocol to let a real feed plug into, with zero Paper Broker changes.
"""

from __future__ import annotations

from decimal import Decimal

from personaltrade.core.enums import Interval
from personaltrade.data.store.candles import CandleStore
from personaltrade.data.store.models import Instrument


class ReplayQuoteSource:
    def __init__(self, candle_store: CandleStore, interval: Interval) -> None:
        self.candle_store = candle_store
        self.interval = interval

    def get_ltp(self, instrument: Instrument) -> Decimal | None:
        frame = self.candle_store.read(instrument.symbol, instrument.exchange, self.interval)
        if frame.empty:
            return None
        return Decimal(str(frame["close"].iloc[-1]))


class LiveQuoteSource:
    """Fed by the live feed's own completed candles, keyed by `instrument_key`
    (the same identifier `CandleReceived`/`Quote` use throughout data/live/ and
    data/providers/) — not `Instrument.symbol`, since `get_ltp` receives the
    full `Instrument` row and reads its `.instrument_key` to look up here."""

    def __init__(self) -> None:
        self._prices: dict[str, Decimal] = {}

    def update(self, instrument_key: str, price: Decimal) -> None:
        self._prices[instrument_key] = price

    def get_ltp(self, instrument: Instrument) -> Decimal | None:
        return self._prices.get(instrument.instrument_key)
