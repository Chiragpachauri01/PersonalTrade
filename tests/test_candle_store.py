from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from personaltrade.core.enums import Interval
from personaltrade.data.store.candles import CandleStore
from tests.factories import daily_frame


def test_write_read_roundtrip(tmp_path: Path) -> None:
    store = CandleStore(tmp_path)
    frame = daily_frame()
    total = store.write("RELIANCE", "NSE", Interval.D1, frame)
    assert total == 13

    loaded = store.read("RELIANCE", "NSE", Interval.D1)
    assert len(loaded) == 13
    assert loaded["ts"].is_monotonic_increasing
    assert str(loaded["ts"].dtype).endswith("UTC]")
    assert loaded["close"].iloc[-1] == 1327.2


def test_rewrite_is_idempotent(tmp_path: Path) -> None:
    store = CandleStore(tmp_path)
    store.write("RELIANCE", "NSE", Interval.D1, daily_frame())
    total = store.write("RELIANCE", "NSE", Interval.D1, daily_frame())  # same data again
    assert total == 13
    assert len(store.read("RELIANCE", "NSE", Interval.D1)) == 13


def test_overlapping_write_keeps_latest_values(tmp_path: Path) -> None:
    store = CandleStore(tmp_path)
    store.write("RELIANCE", "NSE", Interval.D1, daily_frame())
    corrected = daily_frame().tail(1).copy()  # last candle (17 Jul) with a corrected close
    corrected["close"] = 9999.0
    total = store.write("RELIANCE", "NSE", Interval.D1, corrected)
    assert total == 13
    loaded = store.read("RELIANCE", "NSE", Interval.D1)
    assert loaded["close"].iloc[-1] == 9999.0


def test_year_partitioning(tmp_path: Path) -> None:
    store = CandleStore(tmp_path)
    frame = daily_frame(
        [
            ["2025-12-30T00:00:00+05:30", 100.0, 101.0, 99.0, 100.5, 1000, 0],
            ["2026-01-02T00:00:00+05:30", 101.0, 102.0, 100.0, 101.5, 1100, 0],
        ]
    )
    store.write("X", "NSE", Interval.D1, frame)
    dataset = store.dataset_dir("X", "NSE", Interval.D1)
    assert (dataset / "year=2025" / "part.parquet").exists()
    assert (dataset / "year=2026" / "part.parquet").exists()
    assert len(store.read("X", "NSE", Interval.D1)) == 2


def test_read_range_filter(tmp_path: Path) -> None:
    store = CandleStore(tmp_path)
    store.write("RELIANCE", "NSE", Interval.D1, daily_frame())
    part = store.read(
        "RELIANCE",
        "NSE",
        Interval.D1,
        from_ts=datetime(2026, 7, 9, tzinfo=UTC),
        to_ts=datetime(2026, 7, 14, tzinfo=UTC),
    )
    # IST daily candles at 18:30 UTC of the prior day: 9..14 Jul UTC window covers
    # trading days 10, 13, 14 Jul (candles at 09-, 12-, 13- Jul 18:30 UTC)
    assert len(part) == 3


def test_read_missing_dataset_returns_empty(tmp_path: Path) -> None:
    frame = CandleStore(tmp_path).read("NOPE", "NSE", Interval.D1)
    assert frame.empty


def test_manifest_and_datasets_listing(tmp_path: Path) -> None:
    store = CandleStore(tmp_path)
    store.write("RELIANCE", "NSE", Interval.D1, daily_frame(), validation="ok")
    datasets = store.datasets()
    assert len(datasets) == 1
    ds = datasets[0]
    assert (ds.symbol, ds.exchange, ds.interval, ds.rows) == ("RELIANCE", "NSE", Interval.D1, 13)
    assert ds.validation == "ok"
    assert ds.first_ts is not None and ds.first_ts.startswith("2026-06-30T18:30")
    assert ds.last_ts is not None and ds.last_ts.startswith("2026-07-16T18:30")
