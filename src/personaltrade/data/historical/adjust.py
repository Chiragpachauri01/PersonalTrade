"""Corporate-action back-adjustment (deterministic, Rule 9).

Upstox v3 historical candles arrive already adjusted for splits/bonuses
(verified empirically: RELIANCE series is continuous across its 2024 bonus).
This module exists for providers that deliver raw prices, and for re-verifying
vendor adjustment: apply_adjustments() back-adjusts prices before each ex-date.

Convention: `factor` is the price multiplier applied to candles strictly before
`ex_date` (IST trading date). A 1:5 split → factor 0.2; a 1:1 bonus → 0.5.
Volume is divided by the same factor to conserve turnover.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import pandas as pd

from personaltrade.core.calendar import IST

PRICE_COLUMNS = ["open", "high", "low", "close"]


@dataclass(frozen=True)
class CorporateAction:
    ex_date: date
    factor: Decimal  # price multiplier for candles before ex_date
    kind: str = "split"  # split | bonus | other (informational)


def apply_adjustments(frame: pd.DataFrame, actions: list[CorporateAction]) -> pd.DataFrame:
    """Return a back-adjusted copy; input frame is untouched.

    Multiple actions compound (each applies to everything before its own ex-date).
    """
    out = frame.copy()
    if out.empty or not actions:
        return out
    ist_dates = out["ts"].dt.tz_convert(IST).dt.date
    for action in sorted(actions, key=lambda a: a.ex_date):
        factor = float(action.factor)
        if factor <= 0:
            raise ValueError(f"corporate action factor must be > 0, got {action.factor}")
        before = ist_dates < action.ex_date
        out.loc[before, PRICE_COLUMNS] = out.loc[before, PRICE_COLUMNS] * factor
        out.loc[before, "volume"] = (out.loc[before, "volume"] / factor).round().astype("int64")
    return out
