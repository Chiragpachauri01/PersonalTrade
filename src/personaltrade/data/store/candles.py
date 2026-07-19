"""Parquet candle store + DuckDB read layer (ADR-003).

Layout (docs/architecture/02-data-model.md):
    {root}/{exchange}/{symbol}/{interval}/year=YYYY/part.parquet
    {root}/{exchange}/{symbol}/{interval}/manifest.json

Writes merge with existing data per year partition (idempotent re-syncs).
Reads go through DuckDB — the same scan path the backtester will use.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from personaltrade.core.enums import Interval
from personaltrade.data.providers.base import (
    CANDLE_COLUMNS,
    empty_candle_frame,
    normalize_candle_frame,
)

MANIFEST_NAME = "manifest.json"


@dataclass(frozen=True)
class DatasetInfo:
    exchange: str
    symbol: str
    interval: Interval
    rows: int
    first_ts: str | None
    last_ts: str | None
    synced_at: str | None
    validation: str | None


class CandleStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def dataset_dir(self, symbol: str, exchange: str, interval: Interval) -> Path:
        return self.root / exchange / symbol / interval.value

    def write(
        self,
        symbol: str,
        exchange: str,
        interval: Interval,
        frame: pd.DataFrame,
        source: str = "upstox",
        validation: str | None = None,
    ) -> int:
        """Merge candles into the dataset; returns total rows now stored."""
        frame = normalize_candle_frame(frame)
        dataset = self.dataset_dir(symbol, exchange, interval)
        dataset.mkdir(parents=True, exist_ok=True)

        if not frame.empty:
            for year, year_frame in frame.groupby(frame["ts"].dt.year):
                partition = dataset / f"year={year}"
                partition.mkdir(exist_ok=True)
                target = partition / "part.parquet"
                if target.exists():
                    existing = pd.read_parquet(target)
                    year_frame = pd.concat([existing, year_frame], ignore_index=True)
                merged = normalize_candle_frame(year_frame)
                merged.to_parquet(target, index=False)

        total = self._count_rows(dataset)
        self._write_manifest(dataset, symbol, exchange, interval, total, source, validation)
        return total

    def read(
        self,
        symbol: str,
        exchange: str,
        interval: Interval,
        from_ts: datetime | None = None,
        to_ts: datetime | None = None,
    ) -> pd.DataFrame:
        files = self._parquet_files(self.dataset_dir(symbol, exchange, interval))
        if not files:
            return empty_candle_frame()
        con = duckdb.connect()
        try:
            rel = con.read_parquet([str(f) for f in files])
            if from_ts is not None:
                rel = rel.filter(f"ts >= TIMESTAMP WITH TIME ZONE '{_iso_utc(from_ts)}'")
            if to_ts is not None:
                rel = rel.filter(f"ts <= TIMESTAMP WITH TIME ZONE '{_iso_utc(to_ts)}'")
            frame = rel.order("ts").df()
        finally:
            con.close()
        return normalize_candle_frame(frame[CANDLE_COLUMNS])

    def datasets(self) -> list[DatasetInfo]:
        """All stored datasets, discovered via manifests."""
        infos = []
        for manifest_path in sorted(self.root.glob(f"*/*/*/{MANIFEST_NAME}")):
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
            infos.append(
                DatasetInfo(
                    exchange=raw["exchange"],
                    symbol=raw["symbol"],
                    interval=Interval(raw["interval"]),
                    rows=raw["rows"],
                    first_ts=raw.get("first_ts"),
                    last_ts=raw.get("last_ts"),
                    synced_at=raw.get("synced_at"),
                    validation=raw.get("validation"),
                )
            )
        return infos

    def _parquet_files(self, dataset: Path) -> list[Path]:
        return sorted(dataset.glob("year=*/part.parquet"))

    def _count_rows(self, dataset: Path) -> int:
        files = self._parquet_files(dataset)
        if not files:
            return 0
        con = duckdb.connect()
        try:
            result = con.read_parquet([str(f) for f in files]).aggregate("count(*) AS n").fetchone()
        finally:
            con.close()
        return int(result[0]) if result else 0

    def _bounds(self, dataset: Path) -> tuple[str | None, str | None]:
        files = self._parquet_files(dataset)
        if not files:
            return None, None
        con = duckdb.connect()
        try:
            row = (
                con.read_parquet([str(f) for f in files])
                .aggregate("min(ts) AS lo, max(ts) AS hi")
                .fetchone()
            )
        finally:
            con.close()
        if row is None or row[0] is None:
            return None, None
        return _iso_utc(row[0]), _iso_utc(row[1])

    def _write_manifest(
        self,
        dataset: Path,
        symbol: str,
        exchange: str,
        interval: Interval,
        rows: int,
        source: str,
        validation: str | None,
    ) -> None:
        first_ts, last_ts = self._bounds(dataset)
        manifest: dict[str, Any] = {
            "symbol": symbol,
            "exchange": exchange,
            "interval": interval.value,
            "source": source,
            "rows": rows,
            "first_ts": first_ts,
            "last_ts": last_ts,
            "synced_at": datetime.now(UTC).isoformat(),
            "validation": validation,
        }
        (dataset / MANIFEST_NAME).write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )


def _iso_utc(value: datetime | pd.Timestamp) -> str:
    ts = pd.Timestamp(value)
    ts = ts.tz_localize(UTC) if ts.tzinfo is None else ts.tz_convert(UTC)
    return ts.isoformat()
