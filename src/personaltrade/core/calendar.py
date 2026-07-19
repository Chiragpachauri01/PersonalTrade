"""NSE trading calendar — the single authority on trading days (Rule 16).

Weekends are computed; holidays come from config/nse_holidays.yaml so the list
can be refreshed annually without a code change. Timestamps are stored UTC
everywhere; *trading-day* semantics are IST — use ist_trading_date() to map.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from personaltrade.core.errors import ConfigError

IST = ZoneInfo("Asia/Kolkata")

DEFAULT_HOLIDAYS_FILE = Path("config/nse_holidays.yaml")


class NSECalendar:
    def __init__(self, holidays: set[date]) -> None:
        self.holidays = holidays

    @classmethod
    def load(cls, holidays_file: Path | None = None) -> NSECalendar:
        path = holidays_file or DEFAULT_HOLIDAYS_FILE
        if not path.is_file():
            raise ConfigError(f"missing NSE holidays file: {path}")
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        holidays: set[date] = set()
        for _year, days in (raw.get("holidays") or {}).items():
            for d in days or []:
                if not isinstance(d, date):
                    raise ConfigError(f"{path}: expected a date, got {d!r}")
                holidays.add(d)
        return cls(holidays)

    def is_trading_day(self, d: date) -> bool:
        return d.weekday() < 5 and d not in self.holidays

    def trading_days_between(self, start: date, end: date) -> list[date]:
        """All trading days in [start, end], inclusive."""
        if end < start:
            return []
        days = []
        current = start
        while current <= end:
            if self.is_trading_day(current):
                days.append(current)
            current += timedelta(days=1)
        return days

    def previous_trading_day(self, d: date) -> date:
        current = d - timedelta(days=1)
        while not self.is_trading_day(current):
            current -= timedelta(days=1)
        return current

    def next_trading_day(self, d: date) -> date:
        current = d + timedelta(days=1)
        while not self.is_trading_day(current):
            current += timedelta(days=1)
        return current


def ist_trading_date(ts_utc: datetime) -> date:
    """The IST calendar date a UTC timestamp belongs to (candle → trading day)."""
    return ts_utc.astimezone(IST).date()
