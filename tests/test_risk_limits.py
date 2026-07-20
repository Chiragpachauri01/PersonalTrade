from __future__ import annotations

from decimal import Decimal

import pytest

from personaltrade.risk.limits import exceeds_max_daily_loss, exceeds_max_open_positions


class TestExceedsMaxOpenPositions:
    @pytest.mark.parametrize(
        ("open_count", "max_open_positions", "expected"),
        [
            (0, 5, False),
            (4, 5, False),
            (5, 5, True),  # a 6th position would breach — cap already reached
            (6, 5, True),  # already over (e.g. config tightened after the fact)
        ],
    )
    def test_table(self, open_count: int, max_open_positions: int, expected: bool) -> None:
        assert exceeds_max_open_positions(open_count, max_open_positions) is expected


class TestExceedsMaxDailyLoss:
    def test_profit_never_trips(self) -> None:
        assert exceeds_max_daily_loss(Decimal("5000"), Decimal("500000"), Decimal("3.0")) is False

    def test_flat_day_never_trips(self) -> None:
        assert exceeds_max_daily_loss(Decimal("0"), Decimal("500000"), Decimal("3.0")) is False

    def test_loss_under_threshold(self) -> None:
        # -10000 / 500000 = -2% < 3% cap
        assert exceeds_max_daily_loss(Decimal("-10000"), Decimal("500000"), Decimal("3.0")) is False

    def test_loss_exactly_at_threshold_trips(self) -> None:
        # -15000 / 500000 = exactly -3%
        assert exceeds_max_daily_loss(Decimal("-15000"), Decimal("500000"), Decimal("3.0")) is True

    def test_loss_over_threshold_trips(self) -> None:
        assert exceeds_max_daily_loss(Decimal("-20000"), Decimal("500000"), Decimal("3.0")) is True

    def test_non_positive_equity_never_trips(self) -> None:
        # Can't express a percentage against zero/negative equity; the sizer
        # independently refuses to size anything in this case (ZERO_QUANTITY).
        assert exceeds_max_daily_loss(Decimal("-100"), Decimal("0"), Decimal("3.0")) is False
        assert exceeds_max_daily_loss(Decimal("-100"), Decimal("-500"), Decimal("3.0")) is False
