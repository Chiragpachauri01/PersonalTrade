"""NSE trading calendar — the single authority on trading days (Rule 16).

Weekends are computed; holidays come from config/nse_holidays.yaml so the list
can be refreshed annually without a code change. Timestamps are stored UTC
everywhere; *trading-day* semantics are IST — use ist_trading_date() to map.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from personaltrade.core.errors import ConfigError

IST = ZoneInfo("Asia/Kolkata")

DEFAULT_HOLIDAYS_FILE = Path("config/nse_holidays.yaml")

#: Regular NSE equity cash-market session (ROADMAP M10). Pre-open auction
#: (09:00-09:15) and post-close sessions are out of scope — this gates when
#: the live feed should be connected/streaming, not exchange microstructure.
MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)


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

    def is_open_at(self, ts_utc: datetime) -> bool:
        """True if `ts_utc` falls within regular NSE trading hours on a trading day."""
        ist = ts_utc.astimezone(IST)
        return self.is_trading_day(ist.date()) and MARKET_OPEN <= ist.time() <= MARKET_CLOSE


def ist_trading_date(ts_utc: datetime) -> date:
    """The IST calendar date a UTC timestamp belongs to (candle → trading day)."""
    return ts_utc.astimezone(IST).date()


def ist_midnight_utc(ts_utc: datetime) -> datetime:
    """Start of `ts_utc`'s IST trading day, expressed back in UTC (ROADMAP M11:
    the boundary for "today's" realized P&L in the max-daily-loss risk check)."""
    ist_midnight = ts_utc.astimezone(IST).replace(hour=0, minute=0, second=0, microsecond=0)
    return ist_midnight.astimezone(UTC)
