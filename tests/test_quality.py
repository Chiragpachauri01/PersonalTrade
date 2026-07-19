from __future__ import annotations

import pandas as pd

from personaltrade.core.calendar import NSECalendar
from personaltrade.core.enums import Interval
from personaltrade.data.historical.quality import check_candles
from personaltrade.data.providers.base import empty_candle_frame
from tests.factories import RELIANCE_DAILY_CANDLES, daily_frame


def _kinds(frame: pd.DataFrame, calendar: NSECalendar | None = None) -> dict[str, str]:
    report = check_candles(frame, Interval.D1, calendar)
    return {f.kind: f.severity for f in report.findings}


def test_clean_real_series_is_ok() -> None:
    report = check_candles(daily_frame(), Interval.D1, NSECalendar(holidays=set()))
    assert report.status == "ok"
    assert report.summary() == "ok"


def test_empty_frame_is_error() -> None:
    report = check_candles(empty_candle_frame(), Interval.D1)
    assert report.status == "errors"
    assert report.findings[0].kind == "empty"


def test_duplicate_timestamps_detected() -> None:
    rows = [*RELIANCE_DAILY_CANDLES, RELIANCE_DAILY_CANDLES[0]]
    frame = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume", "oi"])
    frame["ts"] = pd.to_datetime(frame["ts"], utc=True)
    frame = frame.sort_values("ts").reset_index(drop=True)  # deliberately NOT deduped
    assert _kinds(frame)["duplicates"] == "error"


def test_bad_ohlc_detected() -> None:
    frame = daily_frame()
    frame.loc[frame.index[3], "high"] = 1.0  # high below open/close
    assert _kinds(frame)["bad_ohlc"] == "error"


def test_negative_volume_detected() -> None:
    frame = daily_frame()
    frame.loc[frame.index[0], "volume"] = -5
    assert _kinds(frame)["negative_volume"] == "error"


def test_price_spike_flagged_as_warning() -> None:
    frame = daily_frame()
    frame.loc[frame.index[6], "close"] = frame["close"].iloc[5] * 2  # +100% day
    kinds = _kinds(frame)
    assert kinds["price_spike"] == "warning"


def test_missing_trading_day_flagged_with_calendar() -> None:
    rows = [r for r in RELIANCE_DAILY_CANDLES if not str(r[0]).startswith("2026-07-09")]
    frame = daily_frame(rows)
    kinds = _kinds(frame, NSECalendar(holidays=set()))
    assert kinds["missing_days"] == "warning"


def test_holiday_gap_not_flagged(tmp_path: object) -> None:
    # remove 9 Jul but declare it a holiday -> no missing_days finding
    rows = [r for r in RELIANCE_DAILY_CANDLES if not str(r[0]).startswith("2026-07-09")]
    frame = daily_frame(rows)
    from datetime import date

    kinds = _kinds(frame, NSECalendar(holidays={date(2026, 7, 9)}))
    assert "missing_days" not in kinds
