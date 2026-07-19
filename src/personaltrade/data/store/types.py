"""Custom SQLAlchemy column types.

Money is Decimal end-to-end (CLAUDE.md conventions). SQLite has no exact decimal
type and SQLAlchemy's Numeric would round-trip through float, so Decimals are
stored as canonical TEXT (ADR-010). Datetimes are stored naive-UTC and returned
timezone-aware; naive datetimes are rejected at the boundary.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import DateTime, String
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator


class MoneyText(TypeDecorator[Decimal]):
    """Exact Decimal storage as TEXT. Rejects float to prevent silent precision loss."""

    impl = String(40)
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Dialect) -> str | None:
        if value is None:
            return None
        if isinstance(value, float):
            raise TypeError("money columns take Decimal, not float")
        if isinstance(value, int | str):
            value = Decimal(value)
        if not isinstance(value, Decimal):
            raise TypeError(f"money columns take Decimal, got {type(value).__name__}")
        return format(value, "f")

    def process_result_value(self, value: str | None, dialect: Dialect) -> Decimal | None:
        return None if value is None else Decimal(value)


class UTCDateTime(TypeDecorator[datetime]):
    """Timezone-aware UTC datetimes; stored naive-UTC in SQLite."""

    impl = DateTime()
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if not isinstance(value, datetime):
            raise TypeError(f"expected datetime, got {type(value).__name__}")
        if value.tzinfo is None:
            raise ValueError("naive datetime rejected — all timestamps must be tz-aware (UTC)")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        return None if value is None else value.replace(tzinfo=UTC)


def utcnow() -> datetime:
    return datetime.now(UTC)
