"""QuoteSource implementations for the Paper Broker.

`ReplayQuoteSource` is the only one that exists before M10 (Live Market Data Feed)
ships: it returns the most recently *synced* candle's close as a stand-in LTP.
Coarse (daily-bar granularity with the data pipeline as it stands today) but
genuinely correct — a real reference price, not a fake one — and honestly scoped:
it's a stopgap the Paper Broker is built to not need to change when M10 lands a
true live quote behind the same `QuoteSource` Protocol (execution/broker.py).
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
