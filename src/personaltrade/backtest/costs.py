"""Indian NSE equity cost model (docs/architecture/ADRS.md ADR-013).

Deterministic, Decimal throughout (CLAUDE.md conventions). Applies the
standard cost stack for one executed leg (one buy or one sell): brokerage
(percentage, flat-capped), STT, stamp duty, exchange transaction charges,
SEBI charges, and GST — levied only on brokerage + exchange + SEBI, never on
STT or stamp duty. Which components apply, and at what rate, depends on side
(buy/sell) and segment (delivery/intraday); see CostConfig for the rates and
their caveats.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from personaltrade.core.config import CostConfig
from personaltrade.core.enums import Segment, Side


@dataclass(frozen=True)
class TradeCosts:
    """Field names match the Trade ORM columns 1:1 (docs/architecture/02-data-model.md)."""

    brokerage: Decimal
    stt: Decimal
    stamp_duty: Decimal
    exchange_charges: Decimal
    sebi_charges: Decimal
    gst: Decimal
    total: Decimal
    net_amount: Decimal  # turnover - total (SELL) or turnover + total (BUY)


def calculate_costs(
    side: Side, price: Decimal, qty: int, segment: Segment, rates: CostConfig
) -> TradeCosts:
    if price <= 0:
        raise ValueError(f"price must be > 0, got {price}")
    if qty <= 0:
        raise ValueError(f"qty must be > 0, got {qty}")

    turnover = price * qty
    brokerage = min(turnover * rates.brokerage_pct, rates.brokerage_max)

    if segment == Segment.DELIVERY:
        stt = turnover * rates.stt_delivery_pct  # both legs
        stamp_duty = (
            turnover * rates.stamp_duty_buy_delivery_pct if side == Side.BUY else Decimal(0)
        )
    else:
        stt = turnover * rates.stt_intraday_sell_pct if side == Side.SELL else Decimal(0)
        stamp_duty = (
            turnover * rates.stamp_duty_buy_intraday_pct if side == Side.BUY else Decimal(0)
        )

    exchange_charges = turnover * rates.exchange_txn_pct
    sebi_charges = turnover * rates.sebi_pct
    gst = (brokerage + exchange_charges + sebi_charges) * rates.gst_pct
    total = brokerage + stt + stamp_duty + exchange_charges + sebi_charges + gst
    net_amount = turnover - total if side == Side.SELL else turnover + total

    return TradeCosts(
        brokerage=brokerage,
        stt=stt,
        stamp_duty=stamp_duty,
        exchange_charges=exchange_charges,
        sebi_charges=sebi_charges,
        gst=gst,
        total=total,
        net_amount=net_amount,
    )
