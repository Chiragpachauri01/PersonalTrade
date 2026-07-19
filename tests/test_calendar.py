from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from personaltrade.core.calendar import NSECalendar, ist_trading_date
from personaltrade.core.errors import ConfigError

XMAS_2025 = date(2025, 12, 25)  # Thursday, NSE holiday


@pytest.fixture()
def calendar() -> NSECalendar:
    return NSECalendar(holidays={XMAS_2025, date(2026, 1, 26)})


def test_weekends_are_not_trading_days(calendar: NSECalendar) -> None:
    assert not calendar.is_trading_day(date(2026, 7, 18))  # Saturday
    assert not calendar.is_trading_day(date(2026, 7, 19))  # Sunday
    assert calendar.is_trading_day(date(2026, 7, 17))  # Friday


def test_holidays_are_not_trading_days(calendar: NSECalendar) -> None:
    assert not calendar.is_trading_day(XMAS_2025)
    assert not calendar.is_trading_day(date(2026, 1, 26))


def test_trading_days_between_skips_weekend_and_holiday(calendar: NSECalendar) -> None:
    # Wed 24 Dec 2025 .. Mon 29 Dec 2025: Thu 25 is a holiday, 27/28 weekend
    days = calendar.trading_days_between(date(2025, 12, 24), date(2025, 12, 29))
    assert days == [date(2025, 12, 24), date(2025, 12, 26), date(2025, 12, 29)]


def test_next_and_previous_trading_day(calendar: NSECalendar) -> None:
    assert calendar.next_trading_day(date(2025, 12, 24)) == date(2025, 12, 26)
    assert calendar.previous_trading_day(date(2025, 12, 26)) == date(2025, 12, 24)
    assert calendar.next_trading_day(date(2026, 7, 17)) == date(2026, 7, 20)  # over weekend


def test_load_from_yaml(tmp_path: Path) -> None:
    holidays_file = tmp_path / "h.yaml"
    holidays_file.write_text(
        "holidays:\n  2026:\n    - 2026-01-26\n    - 2026-12-25\n", encoding="utf-8"
    )
    cal = NSECalendar.load(holidays_file)
    assert not cal.is_trading_day(date(2026, 1, 26))
    assert cal.is_trading_day(date(2026, 1, 27))


def test_load_missing_file_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="missing NSE holidays"):
        NSECalendar.load(tmp_path / "nope.yaml")


def test_repo_holidays_file_parses() -> None:
    cal = NSECalendar.load(Path("config/nse_holidays.yaml"))
    assert not cal.is_trading_day(date(2025, 12, 25))


def test_ist_trading_date_crosses_midnight() -> None:
    # 18:30 UTC == 00:00 IST next day: daily candle for IST 17 Jul stored as 16 Jul 18:30 UTC
    assert ist_trading_date(datetime(2026, 7, 16, 18, 30, tzinfo=UTC)) == date(2026, 7, 17)
    assert ist_trading_date(datetime(2026, 7, 16, 18, 29, tzinfo=UTC)) == date(2026, 7, 16)
