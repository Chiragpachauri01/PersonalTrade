"""Sync orchestration: provider → quality checks → Parquet store → manifest.

Quality warnings never block storage; errors are stored too (data may still be
usable) but the manifest records the failed validation and the caller is told.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy.orm import Session

from personaltrade.core.calendar import NSECalendar
from personaltrade.core.enums import Interval
from personaltrade.core.errors import PersonalTradeError
from personaltrade.core.logging import get_logger
from personaltrade.data.historical.quality import QualityReport, check_candles
from personaltrade.data.providers.base import MarketDataProvider
from personaltrade.data.store.candles import CandleStore
from personaltrade.data.store.models import Instrument
from personaltrade.data.store.repos import InstrumentRepository

logger = get_logger(__name__)


class UnknownSymbol(PersonalTradeError):
    """Symbol not in the instruments table — run `pt data sync-instruments` first."""


@dataclass(frozen=True)
class SyncResult:
    symbol: str
    interval: Interval
    fetched_rows: int
    total_rows: int
    report: QualityReport


def sync_instruments(provider: MarketDataProvider, session: Session, exchange: str = "NSE") -> int:
    """Upsert the exchange's equity instrument master; returns row count."""
    repo = InstrumentRepository(session)
    count = 0
    for info in provider.get_instruments(exchange):
        existing = repo.get_by_instrument_key(info.instrument_key)
        if existing is None:
            repo.add(
                Instrument(
                    symbol=info.symbol,
                    exchange=info.exchange,
                    isin=info.isin,
                    instrument_key=info.instrument_key,
                    tick_size=info.tick_size,
                    lot_size=info.lot_size,
                )
            )
        else:
            existing.symbol = info.symbol
            existing.isin = info.isin
            existing.tick_size = info.tick_size
            existing.lot_size = info.lot_size
            existing.active = True
        count += 1
    logger.info("instruments_synced", exchange=exchange, count=count)
    return count


def sync_candles(
    provider: MarketDataProvider,
    store: CandleStore,
    session: Session,
    symbol: str,
    interval: Interval,
    from_date: date,
    to_date: date,
    calendar: NSECalendar | None = None,
    exchange: str = "NSE",
) -> SyncResult:
    instrument = InstrumentRepository(session).get_by_symbol(symbol, exchange)
    if instrument is None:
        raise UnknownSymbol(
            f"{symbol} ({exchange}) not in instruments table — run `pt data sync-instruments`"
        )

    frame = provider.get_historical_candles(instrument.instrument_key, interval, from_date, to_date)
    report = check_candles(frame, interval, calendar)
    total = store.write(
        symbol, exchange, interval, frame, source="upstox", validation=report.status
    )
    logger.info(
        "candles_synced",
        symbol=symbol,
        interval=interval.value,
        fetched=len(frame),
        total=total,
        validation=report.status,
    )
    return SyncResult(
        symbol=symbol,
        interval=interval,
        fetched_rows=len(frame),
        total_rows=total,
        report=report,
    )
