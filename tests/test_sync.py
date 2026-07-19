from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy.orm import Session

from personaltrade.core.calendar import NSECalendar
from personaltrade.core.enums import Interval
from personaltrade.data.historical.sync import UnknownSymbol, sync_candles, sync_instruments
from personaltrade.data.providers.base import InstrumentInfo
from personaltrade.data.store.candles import CandleStore
from personaltrade.data.store.repos import InstrumentRepository
from tests.factories import daily_frame

RELIANCE_INFO = InstrumentInfo(
    symbol="RELIANCE",
    exchange="NSE",
    isin="INE002A01018",
    instrument_key="NSE_EQ|INE002A01018",
    name="RELIANCE INDUSTRIES LTD",
    tick_size=Decimal("0.1"),
    lot_size=1,
)


class FakeProvider:
    """MarketDataProvider test double."""

    def __init__(
        self, instruments: list[InstrumentInfo], frame: pd.DataFrame | None = None
    ) -> None:
        self._instruments = instruments
        self._frame = frame if frame is not None else daily_frame()
        self.requested_keys: list[str] = []

    def get_instruments(self, exchange: str = "NSE") -> list[InstrumentInfo]:
        return self._instruments

    def get_historical_candles(
        self, instrument_key: str, interval: Interval, from_date: date, to_date: date
    ) -> pd.DataFrame:
        self.requested_keys.append(instrument_key)
        return self._frame


class TestSyncInstruments:
    def test_inserts_then_updates(self, db_session: Session) -> None:
        provider = FakeProvider([RELIANCE_INFO])
        assert sync_instruments(provider, db_session) == 1

        # second run with changed tick size updates in place, no duplicate rows
        updated = InstrumentInfo(
            symbol="RELIANCE",
            exchange="NSE",
            isin="INE002A01018",
            instrument_key="NSE_EQ|INE002A01018",
            name="RELIANCE INDUSTRIES LTD",
            tick_size=Decimal("0.05"),
            lot_size=1,
        )
        assert sync_instruments(FakeProvider([updated]), db_session) == 1

        repo = InstrumentRepository(db_session)
        assert len(repo.list_all()) == 1
        row = repo.get_by_symbol("RELIANCE")
        assert row is not None
        assert row.tick_size == Decimal("0.05")


class TestSyncCandles:
    def test_unknown_symbol_rejected(self, db_session: Session, tmp_path: Path) -> None:
        provider = FakeProvider([])
        store = CandleStore(tmp_path / "candles")
        with pytest.raises(UnknownSymbol, match="sync-instruments"):
            sync_candles(
                provider,
                store,
                db_session,
                "RELIANCE",
                Interval.D1,
                date(2026, 7, 1),
                date(2026, 7, 17),
            )

    def test_happy_path_stores_and_reports(self, db_session: Session, tmp_path: Path) -> None:
        provider = FakeProvider([RELIANCE_INFO])
        sync_instruments(provider, db_session)
        store = CandleStore(tmp_path / "candles")

        result = sync_candles(
            provider,
            store,
            db_session,
            "RELIANCE",
            Interval.D1,
            date(2026, 7, 1),
            date(2026, 7, 17),
            calendar=NSECalendar(holidays=set()),
        )

        assert provider.requested_keys == ["NSE_EQ|INE002A01018"]
        assert result.fetched_rows == 13
        assert result.total_rows == 13
        assert result.report.status == "ok"
        # parquet + manifest actually on disk
        stored = store.read("RELIANCE", "NSE", Interval.D1)
        assert len(stored) == 13
        datasets = store.datasets()
        assert datasets[0].validation == "ok"
