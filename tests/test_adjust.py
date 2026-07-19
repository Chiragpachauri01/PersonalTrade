from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from personaltrade.data.historical.adjust import CorporateAction, apply_adjustments
from tests.factories import daily_frame

FRAME_ROWS = [
    ["2026-07-13T00:00:00+05:30", 2600.0, 2620.0, 2580.0, 2610.0, 1000, 0],
    ["2026-07-14T00:00:00+05:30", 2610.0, 2650.0, 2600.0, 2640.0, 1200, 0],
    # 1:1 bonus ex-date 15 Jul: price halves at source from here on
    ["2026-07-15T00:00:00+05:30", 1320.0, 1330.0, 1310.0, 1325.0, 2500, 0],
    ["2026-07-16T00:00:00+05:30", 1325.0, 1340.0, 1320.0, 1335.0, 2400, 0],
]


def test_back_adjustment_golden() -> None:
    frame = daily_frame(FRAME_ROWS)
    action = CorporateAction(ex_date=date(2026, 7, 15), factor=Decimal("0.5"), kind="bonus")

    adjusted = apply_adjustments(frame, [action])

    # pre-ex-date candles: prices halved, volume doubled
    assert adjusted["close"].iloc[0] == pytest.approx(1305.0)
    assert adjusted["open"].iloc[1] == pytest.approx(1305.0)
    assert adjusted["high"].iloc[1] == pytest.approx(1325.0)
    assert int(adjusted["volume"].iloc[0]) == 2000
    # ex-date onwards untouched
    assert adjusted["close"].iloc[2] == pytest.approx(1325.0)
    assert int(adjusted["volume"].iloc[3]) == 2400
    # series is now continuous: no artificial 50% gap
    returns = adjusted["close"].pct_change().abs().dropna()
    assert (returns < 0.05).all()


def test_input_frame_untouched() -> None:
    frame = daily_frame(FRAME_ROWS)
    apply_adjustments(frame, [CorporateAction(ex_date=date(2026, 7, 15), factor=Decimal("0.5"))])
    assert frame["close"].iloc[0] == 2610.0
    assert int(frame["volume"].iloc[0]) == 1000


def test_no_actions_is_noop() -> None:
    frame = daily_frame(FRAME_ROWS)
    adjusted = apply_adjustments(frame, [])
    assert adjusted["close"].equals(frame["close"])


def test_invalid_factor_rejected() -> None:
    frame = daily_frame(FRAME_ROWS)
    with pytest.raises(ValueError, match="factor must be > 0"):
        apply_adjustments(frame, [CorporateAction(ex_date=date(2026, 7, 15), factor=Decimal("0"))])
