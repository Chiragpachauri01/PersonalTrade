from __future__ import annotations

from decimal import Decimal

import pytest

from personaltrade.risk.sizing import FixedFractionalSizer


def test_hand_computed_allocation() -> None:
    # equity=500000, risk_pct=1% -> allocation=5000; price=1000 -> qty=floor(5000/1000)=5
    sizer = FixedFractionalSizer(Decimal("1.0"))
    assert sizer.size(Decimal("500000"), Decimal("1000")) == 5


def test_floors_partial_shares() -> None:
    # allocation=5000, price=1300 -> 5000/1300=3.846... -> floor=3
    sizer = FixedFractionalSizer(Decimal("1.0"))
    assert sizer.size(Decimal("500000"), Decimal("1300")) == 3


def test_higher_risk_pct_larger_allocation() -> None:
    sizer = FixedFractionalSizer(Decimal("5.0"))
    # allocation=25000, price=1000 -> qty=25
    assert sizer.size(Decimal("500000"), Decimal("1000")) == 25


def test_zero_or_negative_inputs_yield_zero_qty() -> None:
    sizer = FixedFractionalSizer(Decimal("1.0"))
    assert sizer.size(Decimal("0"), Decimal("1000")) == 0
    assert sizer.size(Decimal("500000"), Decimal("0")) == 0
    assert sizer.size(Decimal("-100"), Decimal("1000")) == 0


def test_rejects_invalid_risk_pct() -> None:
    with pytest.raises(ValueError, match="risk_per_trade_pct"):
        FixedFractionalSizer(Decimal("0"))
    with pytest.raises(ValueError, match="risk_per_trade_pct"):
        FixedFractionalSizer(Decimal("101"))
